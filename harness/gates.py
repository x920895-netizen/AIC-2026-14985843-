"""两级门禁：探索筛查（screen）与发布门禁（release）。

- 探索筛查回答“这个方向值不值得继续花钱做实验”；
- 发布门禁回答“这个候选能不能进入当前版本”。

两者共用同一个纯函数 ``evaluate_gate``，只是阈值不同。默认仍为发布门禁，
因此加入本模块不改变历史行为（向后兼容）。
"""

from dataclasses import replace

from .contracts import GateSpec

SCREEN = "screen"
RELEASE = "release"
CUSTOM = "custom"

TOL = 1e-12

#: 探索筛查：允许更强的成本上升与更弱的统计要求，只拦明显劣化。
SCREEN_THRESHOLDS = dict(
    min_em_gain=0.02,
    max_f1_drop=0.02,
    max_regression_rate=0.10,
    max_cost_ratio=2.0,
    require_positive_interval=False,
)

#: 发布门禁：与原型历史默认值完全一致。
RELEASE_THRESHOLDS = dict(
    min_em_gain=0.02,
    max_f1_drop=0.01,
    max_regression_rate=0.05,
    max_cost_ratio=1.2,
    require_positive_interval=True,
)

PRESETS = {SCREEN: SCREEN_THRESHOLDS, RELEASE: RELEASE_THRESHOLDS}


def build_spec(profile, dataset_id, task_ids, model_config, **overrides) -> GateSpec:
    """按档位构造 ``GateSpec``；``overrides`` 仅用于实验，不改变预设本身。

    除五个判据外，还允许覆盖 ``max_total_cost_cny`` / ``repeats`` /
    ``require_offline`` 等公共字段——在线闭环需要把单次验证的预算上限
    与总预算对齐。
    """
    if profile not in PRESETS:
        raise ValueError("未知门禁档位，只能是 screen 或 release")
    params = dict(PRESETS[profile])
    common = {name for name in GateSpec.__dataclass_fields__
              if name not in {"dataset_id", "task_ids", "model_config"}}
    unknown = set(overrides) - common
    if unknown:
        raise ValueError(f"未知门禁参数：{sorted(unknown)}")
    params.update(overrides)
    return GateSpec(dataset_id=dataset_id, task_ids=tuple(task_ids),
                    model_config=model_config, **params)


def _profile_values(spec: GateSpec) -> tuple:
    return tuple(getattr(spec, name) for name in
                 ("min_em_gain", "max_f1_drop", "max_regression_rate", "max_cost_ratio",
                  "require_positive_interval"))


def gate_profile(spec: GateSpec) -> str:
    """从阈值反推档位；与预设不一致时返回 ``custom``，不冒充预设。"""
    values = _profile_values(spec)
    for profile, thresholds in PRESETS.items():
        if values == tuple(thresholds[name] for name in
                           ("min_em_gain", "max_f1_drop", "max_regression_rate", "max_cost_ratio",
                            "require_positive_interval")):
            return profile
    return CUSTOM


def cost_ratio(metrics: dict) -> float:
    """推理成本比；父成本为 0 时按候选是否为 0 处理，避免除零。"""
    value = metrics.get("cost_ratio")
    if value is not None:
        return float(value)
    parent = metrics.get("parent_cost_cny")
    child = metrics.get("candidate_cost_cny")
    if parent is None or child is None:
        raise ValueError("门禁指标缺少成本比或父子成本")
    parent, child = float(parent), float(child)
    if parent <= 0:
        return 1.0 if child <= 0 else float("inf")
    return child / parent


def evaluate_gate(metrics: dict, spec: GateSpec) -> tuple[bool, list[str]]:
    """纯函数门禁：只读指标与阈值，不触碰存储、时间或随机数。

    返回 ``(passed, reasons)``；每条理由都是稳定中文短句，可直接写入决策与凭证。
    """
    reasons: list[str] = []
    delta_em = float(metrics["delta_em"])
    delta_f1 = float(metrics["delta_f1"])
    regression = metrics.get("regression_rate")
    ratio = cost_ratio(metrics)
    interval = metrics.get("interval")
    if delta_em + TOL < spec.min_em_gain:
        reasons.append("主指标EM增益不足")
    if delta_f1 < -spec.max_f1_drop - TOL:
        reasons.append("辅助指标F1退化超限")
    if regression is not None and regression > spec.max_regression_rate + TOL:
        reasons.append("逐题回归率超限")
    if ratio > spec.max_cost_ratio + TOL:
        reasons.append("候选推理成本比超限")
    if spec.require_positive_interval and (interval is None or float(interval[0]) <= 0):
        reasons.append("配对区间不足以支持正增益")
    return (not reasons, reasons)


#: 依赖“有没有足够证据”的判据；其余判据是结构性硬否决（成本、F1、回归）。
STATISTICAL_REASONS = ("主指标EM增益不足", "配对区间不足以支持正增益")


def assess_gate(metrics: dict, spec: GateSpec, *, min_sample: int | None = None) -> dict:
    """在二值门禁之上给出第三态：``pass`` / ``fail`` / ``insufficient``。

    规则：

    - 通过全部判据 → ``pass``；若题数低于 ``min_sample``，额外标注“探索性”；
    - 命中结构性硬否决（F1、回归率、成本比）→ ``fail``，小样本不能洗白硬伤；
    - 只有统计判据未过、题数不足、且观测到正向苗头 → ``insufficient``；
    - 其余情况（含零增益或负增益）→ ``fail``。

    ``evaluate_gate`` 保持逐字不变，所以历史回放与既有凭证不受影响；本函数
    只把“样本量配不上结论”这件事显式写进评价报告。
    """
    from .stats import mde_proportions

    passed, reasons = evaluate_gate(metrics, spec)
    threshold = spec.min_sample if min_sample is None else int(min_sample)
    if threshold < 1:
        raise ValueError("min_sample 必须为正整数")
    count = metrics.get("count")
    if count is None:
        per_task = metrics.get("per_task") or []
        count = len(per_task) if per_task else len(spec.task_ids) * spec.repeats
    count = int(count)
    baseline = metrics.get("parent_em")
    baseline = 0.5 if baseline is None else min(max(float(baseline), 0.0), 1.0)
    discordance = metrics.get("discordant")
    if discordance is not None:
        discordance = (float(discordance) / count) if count else None
    mde = mde_proportions(max(1, count), baseline, discordance=discordance) if count else None
    observed = abs(float(metrics.get("delta_em") or 0.0))
    underpowered = count < threshold
    hard = [reason for reason in reasons if reason not in STATISTICAL_REASONS]
    if passed:
        verdict = "pass"
    elif not hard and underpowered and float(metrics.get("delta_em") or 0.0) > 0:
        # 有正向苗头但样本撑不起结论：记为证据不足，而不是“已证明无增益”。
        verdict = "insufficient"
    else:
        verdict = "fail"
    notes = []
    if underpowered:
        notes.append(f"样本不足（n={count} < {threshold}）：结论只作探索性证据")
    if mde is not None and observed < mde["mde"]:
        notes.append(f"观测效应 {observed*100:.2f}pp 低于该样本 MDE {mde['mde_pp']:.2f}pp")
    evidence = dict(count=count, min_sample=threshold, underpowered=underpowered,
                    observed_delta=float(metrics.get("delta_em") or 0.0),
                    observed_abs_delta=observed,
                    mde=None if mde is None else mde["mde"],
                    mde_pp=None if mde is None else mde["mde_pp"],
                    mde_discordance=None if mde is None else mde["discordance"],
                    below_mde=mde is not None and observed < mde["mde"],
                    hard_vetoes=list(hard),
                    statistical_vetoes=[r for r in reasons if r in STATISTICAL_REASONS],
                    notes=notes)
    return dict(verdict=verdict, passed=verdict == "pass", reasons=list(reasons),
                evidence=evidence)


def spec_with_profile(spec: GateSpec, profile: str) -> GateSpec:
    """在同数据/同模型上切换档位；用于“先筛查、后发布”的两阶段流程。"""
    if profile not in PRESETS:
        raise ValueError("未知门禁档位，只能是 screen 或 release")
    return replace(spec, **PRESETS[profile])
