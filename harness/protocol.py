"""协议：把数据划分、评价器、阈值、重复次数与随机种子冻结成一个对象。

``ProtocolSpec`` 是“这次实验到底允许做什么”的单一事实来源：

- 划分（val / test）冻结，二者不得重叠；
- 评价器、重复次数、bootstrap 种子与重采样次数冻结；
- 门禁阈值与档位冻结；
- 测试集策略冻结（``once``：一生只跑一次；``report-only``：只用于报告）。

导出的 JSON 可以随论文/评审材料一起提交，任何人用 ``ProtocolSpec.load``
重放都能得到相同的条件哈希；决策凭证里的 ``protocol_hash`` 指向它。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from .contracts import EVALUATOR_IDS, GateSpec, content_hash, now
from .gates import PRESETS, RELEASE, RELEASE_THRESHOLDS, SCREEN, build_spec
from .storage import StateConflict

SCHEMA = "protocol/1"
PROFILES = {SCREEN, RELEASE, "custom"}
TEST_POLICIES = {"once", "report-only"}
GATE_FIELDS = ("min_em_gain", "max_f1_drop", "max_regression_rate", "max_cost_ratio",
               "require_positive_interval")
FIELDS = ("schema", "protocol_id", "dataset_id", "val_task_ids", "test_task_ids",
          "evaluator_id", "gate_profile", "thresholds", "repeats", "seed",
          "bootstrap_resamples", "confidence", "min_sample", "test_policy", "created_at")


def _ids(value, name: str, *, allow_empty: bool = False) -> tuple:
    if isinstance(value, (str, bytes)) or value is None:
        raise ValueError(f"{name}必须是任务ID序列")
    items = tuple(value)
    if not items and not allow_empty:
        raise ValueError(f"{name}不能为空")
    if any(not isinstance(item, str) or not item for item in items):
        raise ValueError(f"{name}中的任务ID必须为非空字符串")
    if len(set(items)) != len(items):
        raise ValueError(f"{name}中的任务ID不可重复")
    return items


def _positive_int(value, name: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name}必须为不小于 {minimum} 的整数")
    return value


@dataclass(frozen=True)
class ProtocolSpec:
    """一次实验的冻结协议。"""

    protocol_id: str
    dataset_id: str
    val_task_ids: tuple[str, ...]
    test_task_ids: tuple[str, ...]
    evaluator_id: str = "hotpot-em-f1-v1"
    gate_profile: str = RELEASE
    thresholds: dict = field(default_factory=dict)
    repeats: int = 1
    seed: int = 1
    bootstrap_resamples: int = 2000
    confidence: float = 0.95
    min_sample: int = 30
    test_policy: str = "once"
    created_at: str = ""

    def __post_init__(self):
        if not isinstance(self.protocol_id, str) or not self.protocol_id.strip():
            raise ValueError("protocol_id 不能为空")
        if not isinstance(self.dataset_id, str) or not self.dataset_id:
            raise ValueError("dataset_id 不能为空")
        val = _ids(self.val_task_ids, "验证集")
        # 测试集允许为空：仅做验证期比较的实验可以没有 holdout。
        test = _ids(self.test_task_ids, "测试集", allow_empty=True)
        if set(val) & set(test):
            raise ValueError("验证集与测试集不能重叠")
        if self.evaluator_id not in EVALUATOR_IDS:
            raise ValueError("未知评价协议；协议只接受仓库内置打分器")
        if self.gate_profile not in PROFILES:
            raise ValueError("gate_profile 只能是 screen、release 或 custom")
        if not isinstance(self.thresholds, dict):
            raise ValueError("thresholds 必须是对象")
        unknown = set(self.thresholds) - set(GATE_FIELDS)
        if unknown:
            raise ValueError(f"未知门禁阈值：{sorted(unknown)}")
        self._check_thresholds(self.resolved_thresholds())
        _positive_int(self.repeats, "repeats")
        _positive_int(self.seed, "seed", 0)
        _positive_int(self.bootstrap_resamples, "bootstrap_resamples", 100)
        _positive_int(self.min_sample, "min_sample")
        if not isinstance(self.confidence, (int, float)) or isinstance(self.confidence, bool) \
                or not 0 < float(self.confidence) < 1:
            raise ValueError("confidence 必须在 (0,1) 内")
        if self.test_policy not in TEST_POLICIES:
            raise ValueError("test_policy 只能是 once 或 report-only")
        if not isinstance(self.created_at, str):
            raise ValueError("created_at 必须为字符串")

    @staticmethod
    def _check_thresholds(values: dict) -> None:
        for name in ("min_em_gain", "max_f1_drop", "max_regression_rate", "max_cost_ratio"):
            value = values[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"阈值 {name} 必须为非负数")
        if not isinstance(values["require_positive_interval"], bool):
            raise ValueError("require_positive_interval 必须为布尔值")

    def resolved_thresholds(self) -> dict:
        base = dict(PRESETS.get(self.gate_profile, RELEASE_THRESHOLDS))
        base.update(self.thresholds)
        return base

    @property
    def hash(self) -> str:
        """协议内容哈希；``created_at`` 不参与，保证同协议跨导出可复现。"""
        body = self.to_dict()
        body.pop("created_at", None)
        return content_hash(body)

    def to_dict(self) -> dict:
        return dict(schema=SCHEMA, protocol_id=self.protocol_id, dataset_id=self.dataset_id,
                    val_task_ids=list(self.val_task_ids), test_task_ids=list(self.test_task_ids),
                    evaluator_id=self.evaluator_id, gate_profile=self.gate_profile,
                    thresholds=dict(self.resolved_thresholds()), repeats=self.repeats,
                    seed=self.seed, bootstrap_resamples=self.bootstrap_resamples,
                    confidence=float(self.confidence), min_sample=self.min_sample,
                    test_policy=self.test_policy,
                    created_at=self.created_at or now())

    @classmethod
    def from_dict(cls, value: dict) -> "ProtocolSpec":
        if not isinstance(value, dict):
            raise ValueError("协议必须是 JSON 对象")
        unknown = set(value) - set(FIELDS)
        if unknown:
            raise ValueError(f"协议包含未知字段：{sorted(unknown)}")
        if value.get("schema") not in (None, SCHEMA):
            raise ValueError(f"协议 schema 不受支持：{value.get('schema')}")
        data = {key: value[key] for key in FIELDS if key in value and key != "schema"}
        data["val_task_ids"] = tuple(data.get("val_task_ids") or ())
        data["test_task_ids"] = tuple(data.get("test_task_ids") or ())
        return cls(**data)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "ProtocolSpec":
        return cls.from_dict(json.loads(text))

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_json(), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> "ProtocolSpec":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def updated(self, **changes) -> "ProtocolSpec":
        """派生新协议（例如把 gate_profile 从 screen 换成 release）。"""
        changes.setdefault("created_at", now())
        return replace(self, **changes)

    def gate_spec(self, model_config: dict, *, task_ids=None, dataset_id: str | None = None,
                  budget: float | None = None, require_offline: bool = False) -> GateSpec:
        """构造与协议完全一致的 ``GateSpec``（种子、重复、样本门槛都在内）。"""
        profile = self.gate_profile if self.gate_profile in PRESETS else RELEASE
        overrides = dict(self.resolved_thresholds())
        overrides.update(protocol_id=self.protocol_id, evaluator_id=self.evaluator_id,
                         repeats=self.repeats, seed=self.seed,
                         bootstrap_resamples=self.bootstrap_resamples,
                         min_sample=self.min_sample, require_offline=require_offline)
        if budget is not None:
            overrides["max_total_cost_cny"] = float(budget)
        return build_spec(profile, dataset_id or self.dataset_id,
                          tuple(task_ids if task_ids is not None else self.val_task_ids),
                          model_config, **overrides)

    def test_spec(self, model_config: dict, *, budget: float | None = None,
                  require_offline: bool = False) -> GateSpec:
        return self.gate_spec(model_config, task_ids=self.test_task_ids, budget=budget,
                              require_offline=require_offline)

    @classmethod
    def from_gate_spec(cls, spec: GateSpec, *, test_task_ids=(), protocol_id: str | None = None,
                       confidence: float = 0.95, min_sample: int | None = None,
                       test_policy: str = "once") -> "ProtocolSpec":
        """从门禁条件反推协议；用于非 evolve 路径的凭证记录。"""
        thresholds = {name: getattr(spec, name) for name in GATE_FIELDS}
        profile = RELEASE
        for name, preset in PRESETS.items():
            if all(thresholds[key] == preset[key] for key in GATE_FIELDS):
                profile = name
                break
        else:
            profile = "custom"
        return cls(protocol_id=protocol_id or f"{spec.dataset_id}-{profile}",
                   dataset_id=spec.dataset_id, val_task_ids=tuple(spec.task_ids),
                   test_task_ids=tuple(test_task_ids), evaluator_id=spec.evaluator_id,
                   gate_profile=profile, thresholds=thresholds, repeats=spec.repeats,
                   seed=spec.seed, bootstrap_resamples=spec.bootstrap_resamples,
                   confidence=confidence,
                   min_sample=min_sample if min_sample is not None else spec.min_sample,
                   test_policy=test_policy)


def build_protocol(dataset_id: str, val_task_ids, test_task_ids, *, gate_profile: str = RELEASE,
                   evaluator_id: str = "hotpot-em-f1-v1", repeats: int = 1, seed: int = 1,
                   bootstrap_resamples: int = 2000, confidence: float = 0.95,
                   min_sample: int = 30, test_policy: str = "once",
                   thresholds: dict | None = None, protocol_id: str | None = None) -> ProtocolSpec:
    """校验划分后构造协议；默认发布档、种子 1、2000 次重采样。"""
    return ProtocolSpec(
        protocol_id=protocol_id or f"{dataset_id}-{gate_profile}-v1",
        dataset_id=dataset_id, val_task_ids=tuple(val_task_ids),
        test_task_ids=tuple(test_task_ids), evaluator_id=evaluator_id,
        gate_profile=gate_profile, thresholds=dict(thresholds or {}), repeats=repeats,
        seed=seed, bootstrap_resamples=bootstrap_resamples, confidence=confidence,
        min_sample=min_sample, test_policy=test_policy, created_at=now())


def protocol_warnings(protocol: ProtocolSpec) -> list[str]:
    """不阻断执行、但必须在报告里出现的方法学警告。"""
    warnings = []
    if len(protocol.test_task_ids) < protocol.min_sample:
        warnings.append(f"测试集 n={len(protocol.test_task_ids)} < min_sample={protocol.min_sample}："
                        "只能报告描述性结果，不宣称统计显著")
    if protocol.repeats > 1:
        warnings.append(f"测试集重复 {protocol.repeats} 次：会放大“挑最好一次”的选择空间，"
                        "重复结果必须一并报告")
    if protocol.test_policy == "report-only":
        warnings.append("测试集策略为 report-only：任何轮次都不得用测试集结果做选择")
    if not warnings:
        warnings.append("协议检查通过：划分互斥、评价器可复算、种子与阈值已冻结")
    return warnings


class TestSetGuard:
    """测试集只跑一次的硬约束。

    通过状态库扫描“同一数据集 + 完全相同测试清单”的运行记录；本演化自身
    产生的运行（``own_run_ids``）不计入。协议为 ``once`` 时，发现任何外部
    运行即拒绝继续，避免“多跑几次挑一个好看的结果”。
    """

    def __init__(self, store):
        self.store = store

    def matching_runs(self, protocol: ProtocolSpec, *, statuses=("completed", "running")) -> list[dict]:
        wanted = list(protocol.test_task_ids)
        rows = []
        for run in self.store.list("runs"):
            config = run.get("validation_config") or {}
            if config.get("dataset_id") != protocol.dataset_id:
                continue
            if list(config.get("task_ids") or []) != wanted:
                continue
            if run.get("status") in statuses:
                rows.append(run)
        return rows

    def check(self, protocol: ProtocolSpec, *, own_run_ids=()) -> dict:
        own = {run_id for run_id in own_run_ids if run_id}
        foreign = [run for run in self.matching_runs(protocol) if run["run_id"] not in own]
        if foreign and protocol.test_policy == "once":
            ids = ", ".join(run["run_id"][:18] for run in foreign[:3])
            raise StateConflict(
                f"测试集已被其它运行使用过（{len(foreign)} 次：{ids}）；"
                "协议 test_policy=once 拒绝重复评测——换独立测试集或显式改用新协议")
        return dict(protocol_hash=protocol.hash, policy=protocol.test_policy,
                    test_task_count=len(protocol.test_task_ids), matching_runs=len(foreign),
                    own_runs=len(own), reused=False)
