"""选择偏差审计：比较次数、有效比较次数与族错误率警告。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.selection import (FAMILY_THRESHOLD, SelectionTracker, build_summary)


def round_row(index, candidate, decision="accept", delta=0.05, parent="version-p",
              version=None, p=0.2, with_metrics=True):
    row = dict(round=index, candidate_id=candidate, parent_version=parent,
               candidate_version=version or f"version-{candidate}",
               decision=decision)
    if with_metrics:
        row["metrics"] = dict(delta_em=delta, delta_f1=delta/2)
        row["statistics"] = dict(mcnemar_p=p)
    return row


class BuildSummaryTests(unittest.TestCase):
    def test_counts_comparisons_and_effective_comparisons(self):
        rows = [round_row(1, "c1", "reject", delta=-0.1),
                round_row(2, "c2", "accept", delta=0.2),
                round_row(3, "c3", "insufficient", delta=0.01),
                round_row(4, "c4", "stop", with_metrics=False),
                round_row(5, "c5", "error", with_metrics=False)]
        summary = build_summary(rows)
        self.assertEqual(summary["schema"], "selection/1")
        self.assertEqual(summary["comparisons"], 3, "stop/error 不是比较")
        self.assertEqual(summary["effective_comparisons"], 3)
        self.assertEqual(summary["ineffective_comparisons"], 0)
        self.assertEqual([row["decision"] for row in summary["rows"]],
                         ["reject", "accept", "insufficient"])

    def test_round_without_metrics_is_not_effective(self):
        rows = [round_row(1, "c1", "accept", with_metrics=False),
                round_row(2, "c2", "reject")]
        summary = build_summary(rows)
        self.assertEqual(summary["comparisons"], 2)
        self.assertEqual(summary["effective_comparisons"], 1)
        self.assertEqual(summary["ineffective_comparisons"], 1)
        self.assertFalse(summary["rows"][0]["usable"])
        self.assertTrue(summary["rows"][1]["usable"])

    def test_three_candidates_do_not_trigger_family_warning(self):
        rows = [round_row(i, f"c{i}", "reject") for i in range(1, FAMILY_THRESHOLD+1)]
        summary = build_summary(rows)
        self.assertEqual(summary["distinct_candidates"], 3)
        self.assertIsNone(summary["family_wise_warning"])

    def test_four_candidates_trigger_family_wise_warning(self):
        rows = [round_row(i, f"c{i}", "reject") for i in range(1, 5)]
        summary = build_summary(rows)
        self.assertEqual(summary["distinct_candidates"], 4)
        warning = summary["family_wise_warning"]
        self.assertIsNotNone(warning)
        self.assertIn("4", warning)
        self.assertIn("Bonferroni", warning)
        self.assertIn("α=0.0125", warning)
        self.assertAlmostEqual(summary["bonferroni_alpha"], 0.05/4, places=12)

    def test_five_candidates_report_effective_comparison_count(self):
        rows = [round_row(i, f"c{i}", "accept") for i in range(1, 6)]
        summary = build_summary(rows)
        self.assertEqual(summary["comparisons"], 5)
        self.assertEqual(summary["effective_comparisons"], 5)
        self.assertEqual(summary["distinct_candidates"], 5)
        self.assertIn("已比较 5 个候选", summary["family_wise_warning"])
        self.assertAlmostEqual(summary["bonferroni_alpha"], 0.01, places=12)

    def test_repeated_candidate_validation_is_counted(self):
        rows = [round_row(1, "c1", "reject"), round_row(2, "c1", "reject")]
        summary = build_summary(rows)
        self.assertEqual(summary["comparisons"], 2)
        self.assertEqual(summary["distinct_candidates"], 1)
        self.assertEqual(summary["candidate_counts"], {"c1": 2})
        self.assertEqual(summary["repeated_candidates"], {"c1": 2})

    def test_parent_version_reuse_is_tracked(self):
        rows = [round_row(1, "c1"), round_row(2, "c2"), round_row(3, "c3")]
        summary = build_summary(rows)
        self.assertEqual(summary["version_counts"]["version-p"], 3)
        self.assertEqual(summary["distinct_versions"], 4)

    def test_threshold_is_configurable(self):
        rows = [round_row(i, f"c{i}") for i in range(1, 4)]
        self.assertIsNotNone(build_summary(rows, threshold=2)["family_wise_warning"])
        self.assertIsNone(build_summary(rows, threshold=5)["family_wise_warning"])

    def test_min_p_value_and_row_metrics(self):
        rows = [round_row(1, "c1", p=0.4), round_row(2, "c2", p=0.01)]
        summary = build_summary(rows)
        self.assertEqual(summary["min_p_value"], 0.01)
        self.assertAlmostEqual(summary["rows"][0]["delta_em"], 0.05, places=12)
        self.assertAlmostEqual(summary["rows"][0]["p_value"], 0.4, places=12)

    def test_empty_rounds_yield_zero_summary(self):
        summary = build_summary([])
        self.assertEqual(summary["comparisons"], 0)
        self.assertEqual(summary["effective_comparisons"], 0)
        self.assertIsNone(summary["family_wise_warning"])
        self.assertIsNone(summary["min_p_value"])
        self.assertEqual(summary["bonferroni_alpha"], 0.05)

    def test_rejects_invalid_parameters(self):
        with self.assertRaises(ValueError):
            build_summary([], alpha=0)
        with self.assertRaises(ValueError):
            build_summary([], alpha=1.0)
        with self.assertRaises(ValueError):
            build_summary([], threshold=0)
        with self.assertRaises(ValueError):
            build_summary([], threshold=True)


class SelectionTrackerTests(unittest.TestCase):
    def test_tracker_matches_pure_summary(self):
        tracker = SelectionTracker()
        tracker.record(round_index=1, candidate_id="c1", parent_version="p",
                       candidate_version="v1", decision="reject", delta_em=-0.2)
        tracker.record(round_index=2, candidate_id="c2", parent_version="v1",
                       candidate_version="v2", decision="accept",
                       metrics=dict(delta_em=0.1, delta_f1=0.05))
        summary = tracker.summary()
        expected = build_summary(tracker.rounds)
        self.assertEqual(summary["comparisons"], expected["comparisons"])
        self.assertEqual(summary["effective_comparisons"], expected["effective_comparisons"])
        self.assertEqual(summary["family_wise_warning"], expected["family_wise_warning"])

    def test_tracker_family_warning_appears_after_four_candidates(self):
        tracker = SelectionTracker()
        for index in range(1, 4):
            tracker.record(round_index=index, candidate_id=f"c{index}", parent_version="p",
                           candidate_version=f"v{index}", decision="reject", delta_em=0.0)
            self.assertIsNone(tracker.family_wise_warning)
        tracker.record(round_index=4, candidate_id="c4", parent_version="p",
                       candidate_version="v4", decision="reject", delta_em=0.0)
        self.assertIsNotNone(tracker.family_wise_warning)

    def test_tracker_accepts_explicit_deltas(self):
        tracker = SelectionTracker(alpha=0.1, threshold=2)
        row = tracker.record(round_index=1, candidate_id="c", parent_version="p",
                             candidate_version="v", decision="accept",
                             delta_em=0.3, delta_f1=0.2, p_value=0.01)
        self.assertEqual(row["delta_em"], 0.3)
        summary = tracker.summary()
        self.assertEqual(summary["alpha"], 0.1)
        self.assertEqual(summary["min_p_value"], 0.01)

    def test_tracker_rejects_invalid_alpha(self):
        with self.assertRaises(ValueError):
            SelectionTracker(alpha=-0.1)
        with self.assertRaises(ValueError):
            SelectionTracker(threshold=0)


if __name__ == "__main__":
    unittest.main()
