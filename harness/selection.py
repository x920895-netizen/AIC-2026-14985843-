"""选择偏差审计：每个候选被比较了几次、族错误率是否需要校正。

一轮“提议 → 验证 → 接受/拒绝”就是一次比较。比较次数越多，至少一次偶然
“看起来更好”的概率越高；本模块把这些比较显式记账，并在候选数超过阈值时
给出可执行的校正建议，而不是让调用方凭感觉忽略多重比较。

口径：

- **比较次数**（``comparisons``）：每一轮真正产生指标的比较；
- **有效比较次数**（``effective_comparisons``）：有可用指标、可能改变当前
  版本的比较（运行失败、指标缺失的轮次不计入）；
- **每个候选被比较次数**（``candidate_counts``）：同一候选被重复验证（例如
  断点续跑后重新验证）时计数会大于 1；
- **版本作为父版本参与次数**（``version_counts``）：同一父版本被比较多次，说明它在
  多轮中反复充当对照。该字段只作参考，**不触发**族错误率警告——放大假阳性的是
  “比较了几个不同的候选”，版本复用是正常演化。
"""

from __future__ import annotations

DEFAULT_ALPHA = 0.05
#: 候选数超过该值时输出族错误率警告（3 个以内按探索性比较处理）。
FAMILY_THRESHOLD = 3
#: 判定为“有效比较”的轮次决策。
VALID_DECISIONS = {"accept", "reject", "insufficient"}
#: 无效比较（没有产出指标）的轮次决策。
INVALID_DECISIONS = {"stop", "error", "failed"}


def _family_warning(distinct: int, alpha: float, threshold: int,
                    effective: int | None = None) -> str | None:
    """族错误率警告：只有“被比较的候选数”超过阈值才触发。

    版本数不计入触发条件——同一父版本被多轮使用是正常演化，不代表多做了
    独立假设检验；真正放大假阳性的是“比较了几个不同的候选”。
    """
    if distinct <= threshold:
        return None
    family = max(distinct, int(effective or 0))
    adjusted = alpha / family
    return (f"已比较 {distinct} 个候选（>{threshold} 个，有效比较 {family} 次），族错误率未校正："
            f"单次 α={alpha:g} 下至少一次假阳性的上界约 {min(1.0, family*alpha):.2f}；"
            f"建议按 Bonferroni 使用 α={adjusted:.4f}，或改用独立 holdout 复验")


def _p_value(row: dict) -> float | None:
    """从轮次记录取 McNemar p：既支持演化轮次的 ``statistics.mcnemar_p``，
    也支持 ``SelectionTracker.record`` 直接写入的 ``p_value``。"""
    if row.get("p_value") is not None:
        try:
            return float(row["p_value"])
        except (TypeError, ValueError):
            return None
    statistics = row.get("statistics") or {}
    value = statistics.get("mcnemar_p") if isinstance(statistics, dict) else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def build_summary(rounds, *, alpha: float = DEFAULT_ALPHA,
                  threshold: int = FAMILY_THRESHOLD) -> dict:
    """从演化轮次记录生成选择偏差摘要（纯函数，可对断点续跑结果重算）。"""
    if not 0 < alpha < 1:
        raise ValueError("alpha 必须在 (0,1) 内")
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
        raise ValueError("threshold 必须为正整数")
    rows = []
    candidate_counts: dict[str, int] = {}
    version_counts: dict[str, int] = {}
    for row in rounds or []:
        decision = str(row.get("decision") or "").lower()
        candidate_id = row.get("candidate_id")
        if not candidate_id or decision in INVALID_DECISIONS:
            continue
        metrics = row.get("metrics") or {}
        usable = decision in VALID_DECISIONS and bool(metrics)
        parent_version = row.get("parent_version")
        candidate_version = row.get("candidate_version")
        candidate_counts[candidate_id] = candidate_counts.get(candidate_id, 0) + 1
        if parent_version:
            version_counts[parent_version] = version_counts.get(parent_version, 0) + 1
        if candidate_version:
            version_counts[candidate_version] = version_counts.get(candidate_version, 0) + 1
        rows.append(dict(round=row.get("round"), candidate_id=candidate_id,
                         parent_version=parent_version, candidate_version=candidate_version,
                         decision=decision, delta_em=metrics.get("delta_em"),
                         delta_f1=metrics.get("delta_f1"),
                         p_value=_p_value(row),
                         usable=usable))
    comparisons = len(rows)
    effective = sum(1 for row in rows if row["usable"])
    distinct_candidates = len(candidate_counts)
    distinct_versions = len(version_counts)
    family = max(distinct_candidates, effective)
    warning = _family_warning(distinct_candidates, alpha, threshold, effective)
    repeated = {key: value for key, value in candidate_counts.items() if value > 1}
    p_values = [row["p_value"] for row in rows if row["p_value"] is not None]
    return dict(
        schema="selection/1",
        alpha=alpha,
        family_threshold=threshold,
        comparisons=comparisons,
        effective_comparisons=effective,
        ineffective_comparisons=comparisons - effective,
        distinct_candidates=distinct_candidates,
        distinct_versions=distinct_versions,
        candidate_counts=dict(sorted(candidate_counts.items())),
        version_counts=dict(sorted(version_counts.items())),
        repeated_candidates=repeated,
        family_wise_warning=warning,
        bonferroni_alpha=(alpha / family) if family else alpha,
        min_p_value=min(p_values) if p_values else None,
        rows=rows,
        note="比较次数按轮次计；同一候选重复验证会计入 candidate_counts，"
             "同一父版本被多轮使用会计入 version_counts。",
    )


class SelectionTracker:
    """在线记账器；与 :func:`build_summary` 同一口径，可在每轮后即时查看。"""

    def __init__(self, *, alpha: float = DEFAULT_ALPHA, threshold: int = FAMILY_THRESHOLD):
        if not 0 < alpha < 1:
            raise ValueError("alpha 必须在 (0,1) 内")
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
            raise ValueError("threshold 必须为正整数")
        self.alpha = alpha
        self.threshold = threshold
        self.rounds: list[dict] = []

    def record(self, *, round_index: int, candidate_id: str, parent_version: str,
               candidate_version: str, decision: str, delta_em=None, delta_f1=None,
               p_value=None, metrics: dict | None = None) -> dict:
        row = dict(round=round_index, candidate_id=candidate_id, parent_version=parent_version,
                   candidate_version=candidate_version, decision=decision,
                   delta_em=delta_em if delta_em is not None else (metrics or {}).get("delta_em"),
                   delta_f1=delta_f1 if delta_f1 is not None else (metrics or {}).get("delta_f1"),
                   p_value=p_value, metrics=metrics or {})
        self.rounds.append(row)
        return row

    def summary(self) -> dict:
        return build_summary(self.rounds, alpha=self.alpha, threshold=self.threshold)

    @property
    def family_wise_warning(self) -> str | None:
        return self.summary()["family_wise_warning"]
