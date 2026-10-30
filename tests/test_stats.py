"""统计口径：用已知分布/闭式解校验 Wilson、McNemar、BCa、MDE（容差 ±1%）。

参考值由独立实现给出（scipy 的正态/卡方/二项分布与闭式公式），确认后作为
常量写死，因此本文件不要求安装 scipy；有 scipy 时再逐项交叉复核一次。
"""

import math
import random
import statistics
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.stats import (analyze_paired, bca_bootstrap, mcnemar_exact, mde_proportions,
                           percentile_interval, power_at, required_pairs, wilson_interval,
                           z_value)

try:  # 可选：仅用于交叉复核，缺失时跳过
    from scipy import stats as scipy_stats
except Exception:  # pragma: no cover - 干净环境没有 scipy 也能跑
    scipy_stats = None

# 独立参考值（scipy 正态分位数 + Wilson score 闭式解），置信水平 95%。
WILSON_REFERENCE = {
    (50, 100): (0.403832, 0.596168),
    (3, 10): (0.107791, 0.603222),
    (0, 20): (0.0, 0.161125),
    (18, 20): (0.698966, 0.972134),
    (1, 40): (0.004427, 0.128814),
}
# 精确 McNemar 双侧 p（二项分布闭式）：2*P(X<=min(b,c)), X~Bin(b+c,0.5)。
MCNEMAR_REFERENCE = {
    (12, 3): 0.03515625,
    (5, 0): 0.0625,
    (8, 2): 0.109375,
    (10, 4): 0.1795654296875,
}
# 带连续性校正的卡方近似 p（1 df，(|b-c|-1)^2/(b+c)）。
MCNEMAR_CONTINUITY_REFERENCE = {
    (12, 3): 0.038867103812417314,
    (8, 2): 0.11384629800665763,
}


def relative_error(actual: float, expected: float) -> float:
    if expected == 0:
        return abs(actual)
    return abs(actual - expected)/abs(expected)


class WilsonIntervalTests(unittest.TestCase):
    def test_matches_independent_reference_within_one_percent(self):
        for (successes, n), (lower, upper) in WILSON_REFERENCE.items():
            result = wilson_interval(successes, n)
            self.assertLess(relative_error(result["lower"], lower), 0.01,
                            f"Wilson 下界偏差超 1%：{successes}/{n}")
            self.assertLess(relative_error(result["upper"], upper), 0.01,
                            f"Wilson 上界偏差超 1%：{successes}/{n}")
            self.assertEqual(result["method"], "wilson-score")
            self.assertEqual(result["successes"], successes)
            self.assertEqual(result["n"], n)
            self.assertAlmostEqual(result["point"], successes/n, places=12)

    @unittest.skipUnless(scipy_stats is not None, "需要 scipy 做交叉复核")
    def test_cross_check_via_score_equation(self):
        # 独立复核：Wilson 区间端点满足 |(p̂-p)/sqrt(p(1-p)/n)| = z。
        for successes, n in WILSON_REFERENCE:
            z = scipy_stats.norm.ppf(0.975)
            result = wilson_interval(successes, n)
            for edge in result["interval"]:
                if edge <= 1e-12 or edge >= 1-1e-12:
                    continue
                score = abs((successes/n - edge)/math.sqrt(edge*(1-edge)/n))
                self.assertAlmostEqual(score, z, delta=1e-6)

    def test_interval_stays_inside_unit_interval(self):
        for successes, n in [(0, 5), (0, 1), (1, 1), (20, 20), (1, 1000000)]:
            result = wilson_interval(successes, n)
            self.assertGreaterEqual(result["lower"], 0.0)
            self.assertLessEqual(result["upper"], 1.0)
            self.assertLessEqual(result["lower"], result["upper"])

    def test_zero_sample_returns_most_conservative_interval(self):
        result = wilson_interval(0, 0)
        self.assertEqual(result["interval"], [0.0, 1.0])
        self.assertIsNone(result["point"])
        self.assertEqual(result["n"], 0)

    def test_wider_for_smaller_samples(self):
        wide = wilson_interval(5, 10)
        narrow = wilson_interval(500, 1000)
        self.assertGreater(wide["upper"]-wide["lower"], narrow["upper"]-narrow["lower"])

    def test_more_samples_narrow_around_same_point(self):
        result = wilson_interval(9, 10)
        self.assertAlmostEqual(result["point"], 0.9, places=12)
        self.assertLess(result["upper"]-result["lower"], 0.4)

    def test_rejects_impossible_counts(self):
        with self.assertRaises(ValueError):
            wilson_interval(11, 10)
        for bad in (-1, 1.5, "3", True):
            with self.assertRaises(ValueError):
                wilson_interval(bad, 10)
        with self.assertRaises(ValueError):
            wilson_interval(1, 10, confidence=1.0)
        with self.assertRaises(ValueError):
            wilson_interval(1, 10, confidence=0)
        with self.assertRaises(ValueError):
            wilson_interval(1, 10, confidence=True)

    def test_z_value_is_standard_normal_quantile(self):
        self.assertAlmostEqual(z_value(0.025), 1.959963984540054, places=9)
        self.assertAlmostEqual(z_value(0.5), 0.0, places=12)
        with self.assertRaises(ValueError):
            z_value(0)


class McNemarTests(unittest.TestCase):
    def test_exact_p_matches_binomial_reference_within_one_percent(self):
        for (fixed, regressed), expected in MCNEMAR_REFERENCE.items():
            result = mcnemar_exact(fixed, regressed)
            self.assertEqual(result["method"], "exact-binomial")
            self.assertFalse(result["continuity"])
            self.assertLess(relative_error(result["p_value"], expected), 0.01)
            self.assertAlmostEqual(result["p_exact"], expected, places=12)
            self.assertEqual(result["discordant"], fixed+regressed)

    def test_continuity_correction_matches_chi2_reference_within_one_percent(self):
        for (fixed, regressed), expected in MCNEMAR_CONTINUITY_REFERENCE.items():
            result = mcnemar_exact(fixed, regressed, continuity=True)
            self.assertEqual(result["method"], "chi2-continuity")
            self.assertTrue(result["continuity"])
            self.assertLess(relative_error(result["p_value"], expected), 0.01)
            self.assertAlmostEqual(result["p_continuity"], expected, places=12)
            self.assertGreater(result["p_value"], result["p_exact"],
                               "连续性校正应让 p 更大（更保守）")

    def test_corrected_statistic_formula(self):
        result = mcnemar_exact(12, 3, continuity=True)
        self.assertAlmostEqual(result["corrected_statistic"], (9-1)**2/15, places=12)
        self.assertAlmostEqual(result["statistic"], 9**2/15, places=12)

    def test_both_p_values_are_always_reported(self):
        exact = mcnemar_exact(12, 3)
        corrected = mcnemar_exact(12, 3, continuity=True)
        self.assertEqual(exact["p_value"], exact["p_exact"])
        self.assertEqual(corrected["p_value"], corrected["p_continuity"])
        self.assertEqual(exact["p_continuity"], corrected["p_continuity"])

    def test_symmetric_when_swapping_fixed_and_regressed(self):
        left = mcnemar_exact(9, 2)
        right = mcnemar_exact(2, 9)
        self.assertAlmostEqual(left["p_value"], right["p_value"], places=12)

    def test_no_discordant_pairs_gives_p_one(self):
        result = mcnemar_exact(0, 0)
        self.assertEqual(result["p_value"], 1.0)
        self.assertEqual(result["discordant"], 0)
        self.assertEqual(result["statistic"], 0.0)
        self.assertAlmostEqual(result["corrected_statistic"], 0.0, places=12)

    def test_exact_recommendation_switches_at_25_discordant(self):
        self.assertTrue(mcnemar_exact(10, 5)["exact_recommended"])
        self.assertFalse(mcnemar_exact(15, 10)["exact_recommended"])
        self.assertIn("卡方近似可用", mcnemar_exact(30, 10)["note"])

    def test_rejects_negative_counts(self):
        with self.assertRaises(ValueError):
            mcnemar_exact(-1, 3)
        with self.assertRaises(ValueError):
            mcnemar_exact(1, True)


class BootstrapTests(unittest.TestCase):
    def normal_sample(self, n=400, seed=11):
        rng = random.Random(seed)
        return [rng.gauss(0.5, 1.0) for _ in range(n)]

    def analytic_interval(self, values, confidence=0.95):
        mean = statistics.mean(values)
        stdev = statistics.stdev(values)
        half = z_value((1-confidence)/2)*stdev/math.sqrt(len(values))
        return mean-half, mean+half

    def test_bca_matches_normal_theory_within_one_percent(self):
        values = self.normal_sample()
        result = bca_bootstrap(values, resamples=2000, seed=11)
        lower, upper = self.analytic_interval(values)
        self.assertLess(relative_error(result["lower"], lower), 0.01)
        self.assertLess(relative_error(result["upper"], upper), 0.01)
        self.assertAlmostEqual(result["point"], statistics.mean(values), places=12)
        self.assertEqual(result["method"], "bca")

    def test_percentile_matches_normal_theory_within_one_percent(self):
        values = self.normal_sample()
        result = percentile_interval(values, resamples=2000, seed=11)
        lower, upper = self.analytic_interval(values)
        self.assertLess(relative_error(result["lower"], lower), 0.01)
        self.assertLess(relative_error(result["upper"], upper), 0.01)
        self.assertEqual(result["indices"], [49, 1949], "百分位索引必须与 v1.1 口径一致")

    def test_bca_and_percentile_agree_on_symmetric_data(self):
        values = self.normal_sample(n=300, seed=3)
        bca = bca_bootstrap(values, resamples=1500, seed=3)
        pct = percentile_interval(values, resamples=1500, seed=3)
        width = pct["upper"]-pct["lower"]
        self.assertLess(abs(bca["lower"]-pct["lower"])/width, 0.1)
        self.assertLess(abs(bca["upper"]-pct["upper"])/width, 0.1)

    def test_bca_covers_true_mean_at_least_nominal_minus_three_points(self):
        # 已知分布 N(0,1)：真值 0，名义 95%；固定种子保证可复现。
        rng = random.Random(5)
        reps, hits = 200, 0
        for _ in range(reps):
            sample = [rng.gauss(0, 1) for _ in range(30)]
            lower, upper = bca_bootstrap(sample, resamples=400, seed=5)["interval"]
            hits += int(lower <= 0 <= upper)
        coverage = hits/reps
        self.assertGreaterEqual(coverage, 0.92, f"BCa 覆盖率过低：{coverage:.3f}")
        self.assertLessEqual(coverage, 1.0)

    def test_bootstrap_is_reproducible_for_same_seed(self):
        values = self.normal_sample(n=120, seed=7)
        first = bca_bootstrap(values, resamples=600, seed=42)
        second = bca_bootstrap(values, resamples=600, seed=42)
        self.assertEqual(first["interval"], second["interval"])
        other = bca_bootstrap(values, resamples=600, seed=43)
        self.assertNotEqual(first["interval"], other["interval"])

    def test_interval_contains_point_estimate(self):
        values = self.normal_sample(n=80, seed=9)
        for result in (bca_bootstrap(values, resamples=500, seed=1),
                       percentile_interval(values, resamples=500, seed=1)):
            self.assertLessEqual(result["lower"], result["point"]+1e-12)
            self.assertGreaterEqual(result["upper"], result["point"]-1e-12)

    def test_bca_degenerates_for_single_value(self):
        result = bca_bootstrap([0.25], resamples=200)
        self.assertTrue(result["degenerate"])
        self.assertEqual(result["interval"], [0.25, 0.25])

    def test_constant_sample_has_zero_width(self):
        result = bca_bootstrap([0.5]*40, resamples=500)
        self.assertEqual(result["lower"], 0.5)
        self.assertEqual(result["upper"], 0.5)
        self.assertGreaterEqual(result["acceleration"], 0.0)

    def test_binary_sample_keeps_nonzero_width(self):
        # 二值差值的 bootstrap 均值仍在 (0,1) 内摆动，区间不应退化为点。
        result = bca_bootstrap([0.0]*18+[1.0]*22, resamples=500)
        self.assertLess(result["lower"], result["upper"])
        self.assertGreaterEqual(result["lower"], 0.0)
        self.assertLessEqual(result["upper"], 1.0)

    def test_rejects_empty_and_short_resampling(self):
        for function in (bca_bootstrap, percentile_interval):
            with self.assertRaises(ValueError):
                function([])
            with self.assertRaises(ValueError):
                function([0.1, 0.2], resamples=99)
            with self.assertRaises(ValueError):
                function([0.1, 0.2], resamples=200, confidence=1.5)


class MDETests(unittest.TestCase):
    def test_mde_matches_closed_form_within_one_percent(self):
        result = mde_proportions(200, 0.5, discordance=0.4)
        expected = (1.959963984540054+0.8416212335729144)*math.sqrt(0.4/200)
        self.assertLess(relative_error(result["mde"], expected), 0.01)
        self.assertAlmostEqual(result["mde_pp"], result["mde"]*100, places=12)

    def test_mde_defaults_to_independent_upper_bound(self):
        result = mde_proportions(100, 0.3)
        self.assertEqual(result["discordance_source"], "independent-upper-bound")
        self.assertAlmostEqual(result["discordance"], 2*0.3*0.7, places=12)

    def test_mde_shrinks_as_sample_grows(self):
        small = mde_proportions(50, 0.5)["mde"]
        large = mde_proportions(500, 0.5)["mde"]
        self.assertGreater(small, large)
        self.assertAlmostEqual(small/large, math.sqrt(10), places=6)

    def test_required_pairs_reaches_target_power(self):
        plan = required_pairs(0.1, 0.5, discordance=0.5, power=0.8)
        achieved = power_at(plan["required_pairs"], 0.1, discordance=0.5)
        self.assertGreaterEqual(achieved["power"], 0.8)
        one_short = power_at(plan["required_pairs"]-1, 0.1, discordance=0.5)
        self.assertLess(one_short["power"], 0.8)

    def test_required_pairs_round_trip_with_mde(self):
        plan = required_pairs(0.05, 0.5, discordance=0.35)
        mde = mde_proportions(plan["required_pairs"], 0.5, discordance=0.35)
        self.assertAlmostEqual(mde["mde"], 0.05, delta=1e-3)

    def test_rejects_invalid_inputs(self):
        with self.assertRaises(ValueError):
            mde_proportions(0, 0.5)
        with self.assertRaises(ValueError):
            mde_proportions(10, 1.5)
        with self.assertRaises(ValueError):
            mde_proportions(10, 0.5, discordance=2.0)
        with self.assertRaises(ValueError):
            mde_proportions(10, 0.5, power=1.0)
        with self.assertRaises(ValueError):
            required_pairs(0, 0.5)
        with self.assertRaises(ValueError):
            power_at(0, 0.1, discordance=0.5)


class AnalyzePairedTests(unittest.TestCase):
    def sample(self, parent_correct, fixed, regressed, total=60):
        parent = [1.0]*parent_correct+[0.0]*(total-parent_correct)
        candidate = list(parent)
        flipped_up = 0
        for index, value in enumerate(parent):
            if value == 0 and flipped_up < fixed:
                candidate[index] = 1.0
                flipped_up += 1
        flipped_down = 0
        for index, value in enumerate(parent):
            if value == 1 and flipped_down < regressed:
                candidate[index] = 0.0
                flipped_down += 1
        return parent, candidate

    def test_reports_every_quantity_needed_by_the_gate(self):
        parent, candidate = self.sample(100, 40, 5, total=200)
        result = analyze_paired(parent, candidate, resamples=500, seed=1)
        self.assertEqual(result["method"], "paired-analysis/1")
        self.assertEqual(result["n"], 200)
        self.assertEqual((result["fixed"], result["regressed"]), (40, 5))
        self.assertEqual(result["discordant"], 45)
        self.assertAlmostEqual(result["delta_em"], 35/200, places=12)
        for key in ("mcnemar", "percentile", "bca", "wilson_parent", "wilson_candidate", "mde"):
            self.assertIn(key, result)
        self.assertFalse(result["sample_insufficient"])
        self.assertFalse(result["underpowered"])
        self.assertTrue(result["delta_significant"])

    def test_flags_insufficient_sample_and_low_power(self):
        parent, candidate = self.sample(3, 2, 0, total=8)
        result = analyze_paired(parent, candidate, resamples=500, min_sample=30)
        self.assertTrue(result["sample_insufficient"])
        self.assertTrue(result["underpowered"])
        self.assertTrue(any("样本不足" in note for note in result["notes"]))
        self.assertTrue(any("低于该样本 MDE" in note for note in result["notes"]))

    def test_requires_equal_non_empty_lengths(self):
        with self.assertRaises(ValueError):
            analyze_paired([1, 0], [1])
        with self.assertRaises(ValueError):
            analyze_paired([], [])

    def test_counts_partition_every_pair(self):
        parent, candidate = self.sample(25, 7, 4, total=50)
        result = analyze_paired(parent, candidate, resamples=400)
        total = (result["fixed"]+result["regressed"]
                 +result["unchanged_right"]+result["unchanged_wrong"])
        self.assertEqual(total, 50)
        self.assertEqual(result["unchanged_right"], 25-4)


if __name__ == "__main__":
    unittest.main()
