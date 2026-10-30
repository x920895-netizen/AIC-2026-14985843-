"""版本、经验、候选和离线验证共用的契约与输入校验。"""

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol
from uuid import uuid4


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def nonnegative(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name}必须是有限非负数")
    return float(value)


#: 允许的评价协议。公共门禁默认仍为 Hotpot EM/F1；校园场景走自己的确定性协议，
#: 两者共用同一套版本、预算与发布路径，但分数不可互相比较。
EVALUATOR_IDS = {"hotpot-em-f1-v1", "campus-policy-v1"}


class TargetKind(str, Enum):
    PROMPT = "prompt"
    MEMORY = "memory"
    TOOL = "tool"
    SKILL = "skill"


def validate_artifact(kind: str, value: dict) -> None:
    TargetKind(kind)
    if not isinstance(value, dict):
        raise ValueError("工件内容必须是对象")
    required = {
        "prompt": {"components": dict},
        "memory": {"experience_ids": list, "retrieval_policy": dict, "lifecycle_policy": dict},
        "tool": {"tools": list, "selection_policy": dict, "risk_constraints": dict},
        "skill": {"definitions": list, "selection_policy": dict, "lifecycle_policy": dict},
    }
    for name, expected in required[kind].items():
        if name not in value or not isinstance(value[name], expected):
            raise ValueError(f"{kind}工件缺少有效的{name}")
    if kind == "prompt" and (not value["components"] or
        any(not isinstance(k, str) or not isinstance(v, str) for k, v in value["components"].items())):
        raise ValueError("Prompt必须包含组件名到指令文本的映射")
    if kind == "memory" and (any(not isinstance(x, str) or not x for x in value["experience_ids"]) or
                              len(value["experience_ids"]) != len(set(value["experience_ids"]))):
        raise ValueError("经验引用必须为不重复的ID")
    if kind == "tool" and any(not isinstance(x, str) or not x for x in value["tools"]):
        raise ValueError("工具必须用具名工具ID引用")
    if kind == "skill":
        for definition in value["definitions"]:
            if not isinstance(definition, dict) or not all(k in definition for k in
                    ["skill_id", "version", "applicable_if", "evidence_refs", "status", "steps"]):
                raise ValueError("技能定义必须含身份、版本、适用条件、证据、状态和步骤")
    canonical(value)


@dataclass(frozen=True)
class EvidenceRef:
    run_id: str
    task_id: str
    repeat: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeTask:
    task_id: str
    question: str

    def __post_init__(self):
        if not self.task_id or not isinstance(self.question, str):
            raise ValueError("任务必须有ID和问题文本")


@dataclass(frozen=True)
class GateSpec:
    dataset_id: str
    task_ids: tuple[str, ...]
    model_config: dict
    protocol_id: str = "offline-paired-v1"
    evaluator_id: str = "hotpot-em-f1-v1"
    repeats: int = 1
    min_em_gain: float = 0.02
    max_f1_drop: float = 0.01
    max_regression_rate: float = 0.05
    max_cost_ratio: float = 1.2
    max_total_cost_cny: float = 1.0
    require_positive_interval: bool = True
    #: True 时强制离线（mode=simulation、零 API 调用、零真实费用）；
    #: 在线闭环必须设为 False，否则预算与调用计数会被门禁自己挡掉。
    require_offline: bool = False
    #: 配对 bootstrap 的随机种子与重采样次数；写进条件哈希，保证区间可复算。
    seed: int = 1
    bootstrap_resamples: int = 2000
    #: 低于该题数时门禁输出“证据不足”，而不是把噪声当结论（见 ``assess_gate``）。
    min_sample: int = 30

    def __post_init__(self):
        if self.evaluator_id not in EVALUATOR_IDS:
            raise ValueError("未知评价协议；公共门禁不接受调用方自定义打分器")
        if not self.dataset_id or not self.task_ids or len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError("验证集须明确且ID不可重复")
        if isinstance(self.repeats, bool) or self.repeats < 1 or not isinstance(self.repeats, int):
            raise ValueError("重复次数须为正整数")
        if not isinstance(self.require_offline, bool):
            raise ValueError("require_offline 必须为布尔值")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise ValueError("bootstrap 种子必须为非负整数")
        if (isinstance(self.bootstrap_resamples, bool) or not isinstance(self.bootstrap_resamples, int)
                or self.bootstrap_resamples < 100):
            raise ValueError("bootstrap 重采样次数至少为 100")
        if (isinstance(self.min_sample, bool) or not isinstance(self.min_sample, int)
                or self.min_sample < 1):
            raise ValueError("min_sample 必须为正整数")
        for name in ["min_em_gain", "max_f1_drop", "max_regression_rate", "max_cost_ratio", "max_total_cost_cny"]:
            nonnegative(getattr(self, name), name)
        canonical(self.model_config)

    def to_dict(self) -> dict:
        value = asdict(self)
        value["task_ids"] = list(self.task_ids)
        return value

    @classmethod
    def from_dict(cls, value: dict) -> "GateSpec":
        return cls(**dict(value, task_ids=tuple(value["task_ids"])))

    @classmethod
    def release(cls, dataset_id: str, task_ids, model_config: dict, **overrides) -> "GateSpec":
        """发布门禁预设（与历史默认值一致）。"""
        from .gates import build_spec, RELEASE
        return build_spec(RELEASE, dataset_id, task_ids, model_config, **overrides)

    @classmethod
    def screen(cls, dataset_id: str, task_ids, model_config: dict, **overrides) -> "GateSpec":
        """探索筛查预设：只拦明显劣化，不要求配对区间为正。"""
        from .gates import build_spec, SCREEN
        return build_spec(SCREEN, dataset_id, task_ids, model_config, **overrides)


@dataclass(frozen=True)
class CandidateProposal:
    parent_version_id: str
    target: TargetKind
    artifact_ref: dict
    gate: GateSpec
    basis: str
    experience_ids: tuple[str, ...] = ()
    expected_benefit: str = "待离线验证"
    risks: tuple[str, ...] = ()
    candidate_id: str = field(default_factory=lambda: new_id("candidate"))


class OfflineRunner(Protocol):
    """只接收运行问题/版本，不接收参考答案；具体演化策略在后续阶段接入。"""

    def __call__(self, task: RuntimeTask, version: dict, artifacts: dict) -> str: ...
