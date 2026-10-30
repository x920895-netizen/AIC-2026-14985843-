"""统计口径：Wilson 区间、精确 McNemar、BCa bootstrap 与最小可检测效应（MDE）。

本模块只做纯计算，不读状态、不碰时间，也不采样随机种子以外的随机源；
所有函数都对输入做显式校验，避免把缺失样本当成“零效应”静默通过。

口径约定（与 ``docs/statistics.md`` 一致）：

- 比例区间用 Wilson score，不用 Wald——小样本、接近 0/1 时不产生负端点；
- 配对二分类用精确 McNemar（二项检验），``continuity=True`` 时改用
  带连续性校正的卡方近似（1 自由度）；
- 差值区间用配对 bootstrap：``percentile`` 与门禁历史口径一致，
  ``bca`` 作为校偏补充，两者都写进评价报告；
- MDE 为“在给定题数、基线错误率与不一致率下，以 80% 把握能检出的
  最小绝对提升”，用于回答“这个样本量到底能说明什么”。
"""

from __future__ import annotations

import math
import random
from statistics import NormalDist

NORMAL = NormalDist()

DEFAULT_CONFIDENCE = 0.95
DEFAULT_POWER = 0.8
DEFAULT_RESAMPLES = 2000
DEFAULT_SEED = 1

#: 低于该配对题数时，精确检验是唯一推荐口径（卡方近似不可靠）。
EXACT_RECOMMENDED_BELOW = 25


def _confidence(confidence: float) -> float:
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("置信水平必须为 (0,1) 内的数值")
    confidence = float(confidence)
    if not 0 < confidence < 1:
        raise ValueError("置信水平必须为 (0,1) 内的数值")
    return confidence


def _count(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name}必须为非负整数")
    return value


def z_value(alpha: float) -> float:
    """标准正态上侧分位数：返回满足 ``P(Z<=z)=1-alpha`` 的 z。"""
    if not 0 < alpha < 1:
        raise ValueError("alpha 必须为 (0,1) 内的数值")
    return NORMAL.inv_cdf(1 - alpha)


# ---------------------------------------------------------------------------
# 比例区间
# ---------------------------------------------------------------------------

def wilson_interval(successes: int, n: int, confidence: float = DEFAULT_CONFIDENCE) -> dict:
    """Wilson score 区间；``n=0`` 时返回最保守的 [0,1]。"""
    successes = _count(successes, "成功数")
    n = _count(n, "样本数")
    if successes > n:
        raise ValueError("成功数不能大于样本数")
    confidence = _confidence(confidence)
    if n == 0:
        return dict(method="wilson-score", successes=0, n=0, point=None,
                    lower=0.0, upper=1.0, confidence=confidence, interval=[0.0, 1.0])
    z = z_value((1 - confidence) / 2)
    p = successes / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    lower, upper = max(0.0, center - half), min(1.0, center + half)
    return dict(method="wilson-score", successes=successes, n=n, point=p,
                lower=lower, upper=upper, confidence=confidence, interval=[lower, upper])


# ---------------------------------------------------------------------------
# 配对二分类：McNemar
# ---------------------------------------------------------------------------

def mcnemar_exact(fixed: int, regressed: int, *, continuity: bool = False) -> dict:
    """配对 McNemar 检验：``fixed`` 为由错变对，``regressed`` 为由对变错。

    默认返回精确二项检验（双侧）；``continuity=True`` 时返回带连续性校正的
    卡方近似（1 自由度，``(|b-c|-1)^2/(b+c)``）。两种口径都保留在结果里，
    调用方选择的是 ``p_value``，另一种在 ``p_exact`` / ``p_continuity`` 中。
    """
    fixed = _count(fixed, "修复数")
    regressed = _count(regressed, "回归数")
    discordant = fixed + regressed
    if discordant == 0:
        p_exact = 1.0
    else:
        tail = sum(math.comb(discordant, k) for k in range(min(fixed, regressed) + 1)) / (2 ** discordant)
        p_exact = min(1.0, 2 * tail)
    statistic = (abs(fixed - regressed) ** 2 / discordant) if discordant else 0.0
    corrected = max(0.0, abs(fixed - regressed) - 1) ** 2 / discordant if discordant else 0.0
    p_continuity = math.erfc(math.sqrt(corrected / 2)) if discordant else 1.0
    if continuity:
        method, p_value = "chi2-continuity", p_continuity
    else:
        method, p_value = "exact-binomial", p_exact
    return dict(method=method, continuity=bool(continuity), fixed=fixed, regressed=regressed,
                discordant=discordant, statistic=statistic, corrected_statistic=corrected,
                p_value=p_value, p_exact=p_exact, p_continuity=p_continuity,
                exact_recommended=discordant < EXACT_RECOMMENDED_BELOW,
                note=("不一致对数较少，建议使用精确检验" if discordant < EXACT_RECOMMENDED_BELOW
                      else "不一致对数充足，卡方近似可用"))


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _quantile(sorted_values: list[float], q: float) -> float:
    """线性插值分位数（与 numpy 默认 ``linear`` 一致）。"""
    if not sorted_values:
        raise ValueError("空样本无法计算分位数")
    if len(sorted_values) == 1:
        return sorted_values[0]
    q = min(max(q, 0.0), 1.0)
    position = q * (len(sorted_values) - 1)
    lower = int(math.floor(position))
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def percentile_interval(values, *, resamples: int = DEFAULT_RESAMPLES,
                        confidence: float = DEFAULT_CONFIDENCE, seed: int = DEFAULT_SEED) -> dict:
    """配对百分位 bootstrap，索引口径与 v1.1 门禁完全一致（默认 49/1949）。"""
    values = [float(v) for v in values]
    if not values:
        raise ValueError("配对差值不能为空")
    resamples = _count(resamples, "重采样次数")
    if resamples < 100:
        raise ValueError("重采样次数至少为 100")
    confidence = _confidence(confidence)
    rng = random.Random(seed)
    n = len(values)
    samples = sorted(sum(rng.choices(values, k=n)) / n for _ in range(resamples))
    lower_index = max(0, min(resamples - 1, int((1 - confidence) / 2 * resamples) - 1))
    upper_index = max(0, min(resamples - 1, int((1 + confidence) / 2 * resamples) - 1))
    return dict(method="paired-percentile", point=_mean(values), lower=samples[lower_index],
                upper=samples[upper_index], indices=[lower_index, upper_index],
                resamples=resamples, seed=seed, confidence=confidence,
                interval=[samples[lower_index], samples[upper_index]])


def bca_bootstrap(values, *, statistic=None, resamples: int = DEFAULT_RESAMPLES,
                  confidence: float = DEFAULT_CONFIDENCE, seed: int = DEFAULT_SEED) -> dict:
    """BCa（偏差校正 + 加速）bootstrap 区间。

    默认统计量是均值；也接受任意 ``statistic(list)->float``。二值差值存在大量
    并列值，偏差比例按“严格小于 + 0.5×并列”计算，避免 z0 被并列压到 ±∞。
    """
    values = [float(v) for v in values]
    if not values:
        raise ValueError("配对差值不能为空")
    resamples = _count(resamples, "重采样次数")
    if resamples < 100:
        raise ValueError("重采样次数至少为 100")
    confidence = _confidence(confidence)
    stat = statistic or _mean
    n = len(values)
    observed = stat(values)
    if n < 2:
        return dict(method="bca", point=observed, lower=observed, upper=observed,
                    resamples=resamples, seed=seed, confidence=confidence, z0=0.0,
                    acceleration=0.0, degenerate=True,
                    interval=[observed, observed], note="n<2，BCa 退化为点估计")
    rng = random.Random(seed)
    boot = sorted(stat(rng.choices(values, k=n)) for _ in range(resamples))
    less = sum(1 for value in boot if value < observed)
    ties = sum(1 for value in boot if value == observed)
    proportion = (less + 0.5 * ties) / resamples
    proportion = min(max(proportion, 1 / (resamples + 1)), resamples / (resamples + 1))
    z0 = NORMAL.inv_cdf(proportion)
    jackknife = [stat(values[:i] + values[i + 1:]) for i in range(n)]
    jack_mean = sum(jackknife) / n
    squared = sum((jack_mean - value) ** 2 for value in jackknife)
    cube = sum((jack_mean - value) ** 3 for value in jackknife)
    acceleration = cube / (6 * squared ** 1.5) if squared > 0 else 0.0
    alpha = (1 - confidence) / 2

    def adjust(z_alpha: float) -> float:
        denominator = 1 - acceleration * (z0 + z_alpha)
        if abs(denominator) < 1e-12:
            return 0.5
        return NORMAL.cdf(z0 + (z0 + z_alpha) / denominator)

    lower_q = adjust(NORMAL.inv_cdf(alpha))
    upper_q = adjust(NORMAL.inv_cdf(1 - alpha))
    lower, upper = _quantile(boot, lower_q), _quantile(boot, upper_q)
    if lower > upper:
        lower, upper = upper, lower
    return dict(method="bca", point=observed, lower=lower, upper=upper,
                resamples=resamples, seed=seed, confidence=confidence, z0=z0,
                acceleration=acceleration, proportions=[lower_q, upper_q],
                degenerate=False, interval=[lower, upper])


# ---------------------------------------------------------------------------
# 最小可检测效应
# ---------------------------------------------------------------------------

def mde_proportions(n: int, baseline_rate: float = 0.5, *, discordance: float | None = None,
                    confidence: float = DEFAULT_CONFIDENCE, power: float = DEFAULT_POWER) -> dict:
    """配对二分类的 MDE（绝对提升，0-1 尺度）。

    方差按 ``MDE ≈ (z_{1-α/2}+z_{power})·sqrt(p_disc/n)``；``p_disc`` 为
    不一致率，未给出时取独立假设下的上界 ``2p(1-p)``（保守）。
    """
    n = _count(n, "样本数")
    if n == 0:
        raise ValueError("样本数必须为正")
    if not 0 <= float(baseline_rate) <= 1:
        raise ValueError("基线正确率必须在 [0,1] 内")
    confidence = _confidence(confidence)
    if not 0 < power < 1:
        raise ValueError("power 必须在 (0,1) 内")
    baseline_rate = float(baseline_rate)
    if discordance is None:
        discordance = 2 * baseline_rate * (1 - baseline_rate)
        source = "independent-upper-bound"
    else:
        if not 0 <= float(discordance) <= 1:
            raise ValueError("不一致率必须在 [0,1] 内")
        discordance = float(discordance)
        source = "given"
    z_alpha = z_value((1 - confidence) / 2)
    z_power = NORMAL.inv_cdf(power)
    effect = (z_alpha + z_power) * math.sqrt(discordance / n) if discordance > 0 else 0.0
    return dict(method="paired-normal-approx", n=n, baseline_rate=baseline_rate,
                discordance=discordance, discordance_source=source,
                mde=effect, mde_pp=effect * 100, alpha=1 - confidence, power=power,
                z_alpha=z_alpha, z_power=z_power)


def required_pairs(effect: float, baseline_rate: float = 0.5, *, discordance: float | None = None,
                   confidence: float = DEFAULT_CONFIDENCE, power: float = DEFAULT_POWER) -> dict:
    """要检出给定绝对提升所需的配对题数（向上取整）。"""
    effect = float(effect)
    if not 0 < effect <= 1:
        raise ValueError("目标效应必须在 (0,1] 内")
    if discordance is None:
        discordance = 2 * baseline_rate * (1 - baseline_rate)
    if not 0 < float(discordance) <= 1:
        raise ValueError("不一致率必须在 (0,1] 内")
    z_alpha = z_value((1 - _confidence(confidence)) / 2)
    z_power = NORMAL.inv_cdf(power)
    n = math.ceil(((z_alpha + z_power) / effect) ** 2 * float(discordance))
    return dict(required_pairs=n, effect=effect, discordance=float(discordance),
                confidence=confidence, power=power)


def power_at(n: int, effect: float, *, discordance: float, confidence: float = DEFAULT_CONFIDENCE) -> dict:
    """给定题数与不一致率时，检出 ``effect`` 的近似把握度。"""
    n = _count(n, "样本数")
    if n == 0 or not 0 < float(discordance) <= 1:
        raise ValueError("样本数须为正且不一致率在 (0,1] 内")
    z_alpha = z_value((1 - _confidence(confidence)) / 2)
    z = float(effect) / math.sqrt(float(discordance) / n)
    return dict(n=n, effect=float(effect), discordance=float(discordance),
                power=NORMAL.cdf(z - z_alpha), z=z, alpha=1 - confidence)


# ---------------------------------------------------------------------------
# 组合分析
# ---------------------------------------------------------------------------

def analyze_paired(parent_scores, candidate_scores, *, confidence: float = DEFAULT_CONFIDENCE,
                   resamples: int = DEFAULT_RESAMPLES, seed: int = DEFAULT_SEED,
                   min_sample: int = 30) -> dict:
    """对逐题配对得分做一次完整的可复核分析。

    ``parent_scores`` / ``candidate_scores`` 是 0/1 序列（重复评测时同一题可出现
    多次，按独立配对行处理）。返回 Mackinnon 之外的常用量：两版 Wilson 区间、
    固定/回归计数、精确 McNemar、百分位与 BCa 区间、MDE 与样本充分性提示。
    """
    parent = [1.0 if float(v) >= 0.5 else 0.0 for v in parent_scores]
    candidate = [1.0 if float(v) >= 0.5 else 0.0 for v in candidate_scores]
    if len(parent) != len(candidate) or not parent:
        raise ValueError("父子得分必须等长且非空")
    n = len(parent)
    fixed = sum(1 for p, c in zip(parent, candidate) if p == 0 and c == 1)
    regressed = sum(1 for p, c in zip(parent, candidate) if p == 1 and c == 0)
    unchanged_right = sum(1 for p, c in zip(parent, candidate) if p == 1 and c == 1)
    unchanged_wrong = sum(1 for p, c in zip(parent, candidate) if p == 0 and c == 0)
    parent_em = sum(parent) / n
    candidate_em = sum(candidate) / n
    deltas = [c - p for p, c in zip(parent, candidate)]
    mcnemar = mcnemar_exact(fixed, regressed)
    mde = mde_proportions(n, parent_em, discordance=(fixed + regressed) / n)
    notes = []
    if n < min_sample:
        notes.append(f"样本不足（n={n} < {min_sample}）：统计判据只能给出探索性结论")
    if abs(candidate_em - parent_em) < mde["mde"]:
        notes.append(f"观测效应 {abs(candidate_em-parent_em)*100:.2f}pp 低于该样本 MDE "
                     f"{mde['mde_pp']:.2f}pp，不能据此宣称方向")
    if mcnemar["p_value"] >= 1 - confidence:
        notes.append(f"精确 McNemar p={mcnemar['p_value']:.4f}，未达显著")
    return dict(
        method="paired-analysis/1", n=n, repeats=1,
        parent_em=parent_em, candidate_em=candidate_em, delta_em=candidate_em - parent_em,
        fixed=fixed, regressed=regressed, unchanged_right=unchanged_right,
        unchanged_wrong=unchanged_wrong, discordant=fixed + regressed,
        mcnemar=mcnemar,
        percentile=percentile_interval(deltas, resamples=resamples, confidence=confidence, seed=seed),
        bca=bca_bootstrap(deltas, resamples=resamples, confidence=confidence, seed=seed),
        wilson_parent=wilson_interval(int(sum(parent)), n, confidence),
        wilson_candidate=wilson_interval(int(sum(candidate)), n, confidence),
        mde=mde, notes=notes,
        delta_significant=mcnemar["p_value"] < 1 - confidence,
        underpowered=abs(candidate_em - parent_em) < mde["mde"],
        sample_insufficient=n < min_sample,
    )
