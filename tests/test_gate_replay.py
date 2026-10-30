"""历史门禁回放：数字、决策与证据自洽性全部锁死。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "examples"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import gate_replay


class GateReplayNumbersTests(unittest.TestCase):
    def test_r1_metrics_match_facts(self):
        metrics = gate_replay.metrics_from(gate_replay.load_experiment("R1"))
        self.assertAlmostEqual(metrics["parent_em"], 19 / 30, places=12)
        self.assertAlmostEqual(metrics["candidate_em"], 23 / 30, places=12)
        self.assertAlmostEqual(metrics["delta_em"], 0.133333333333, places=9)
        self.assertAlmostEqual(metrics["delta_f1"], 0.11918263090676884, places=12)
        self.assertEqual(metrics["regression_rate"], 0.0)
        self.assertAlmostEqual(metrics["cost_ratio"], 1.6476897580877794, places=12)
        self.assertAlmostEqual(metrics["interval"][0], 0.03333333333333333, places=12)
        self.assertAlmostEqual(metrics["interval"][1], 0.26666666666666666, places=12)

    def test_r2_metrics_match_facts(self):
        metrics = gate_replay.metrics_from(gate_replay.load_experiment("R2"))
        self.assertAlmostEqual(metrics["parent_em"], 23 / 30, places=12)
        self.assertAlmostEqual(metrics["candidate_em"], 21 / 30, places=12)
        self.assertAlmostEqual(metrics["delta_em"], -0.06666666666666665, places=12)
        self.assertAlmostEqual(metrics["delta_f1"], -0.010980392156862862, places=12)
        self.assertAlmostEqual(metrics["regression_rate"], 2 / 23, places=12)
        self.assertAlmostEqual(metrics["cost_ratio"], 1.1305727187430137, places=12)
        self.assertAlmostEqual(metrics["interval"][0], -0.16666666666666666, places=12)
        self.assertAlmostEqual(metrics["interval"][1], 0.0, places=12)

    def test_bootstrap_constants_are_the_harness_method(self):
        self.assertEqual(gate_replay.BOOTSTRAP_SEED, 1)
        self.assertEqual(gate_replay.BOOTSTRAP_RESAMPLES, 2000)
        self.assertEqual((gate_replay.LOWER_INDEX, gate_replay.UPPER_INDEX), (49, 1949))
        deltas = gate_replay.paired_deltas(gate_replay.load_experiment("R1"))
        self.assertEqual(deltas.count(1.0), 4)
        self.assertEqual(deltas.count(0.0), 26)


class GateReplayDecisionTests(unittest.TestCase):
    def setUp(self):
        self.results = {name: gate_replay.replay(gate_replay.load_experiment(name))
                        for name in ("R1", "R2")}

    def test_r1_passes_screen_but_release_blocked_by_cost_only(self):
        screen, release = self.results["R1"]["decisions"]["screen"], self.results["R1"]["decisions"]["release"]
        self.assertTrue(screen["passed"])
        self.assertEqual(screen["reasons"], [])
        self.assertFalse(release["passed"])
        self.assertEqual(release["reasons"], ["候选推理成本比超限"])

    def test_r2_fails_screen_on_em_gain(self):
        screen = self.results["R2"]["decisions"]["screen"]
        self.assertFalse(screen["passed"])
        self.assertEqual(screen["reasons"], ["主指标EM增益不足"])

    def test_r2_release_hits_exactly_four_vetoes(self):
        release = self.results["R2"]["decisions"]["release"]
        self.assertFalse(release["passed"])
        self.assertEqual(release["reasons"], ["主指标EM增益不足", "辅助指标F1退化超限",
                                              "逐题回归率超限", "配对区间不足以支持正增益"])
        self.assertEqual(len(release["reasons"]), 4)

    def test_expectations_are_enforced_by_the_script(self):
        for name, result in self.results.items():
            with self.subTest(experiment=name):
                gate_replay.check_expectations(name, result)
        broken = json.loads(json.dumps(self.results))
        broken["R1"]["decisions"]["release"]["reasons"] = []
        with self.assertRaises(AssertionError):
            gate_replay.check_expectations("R1", broken["R1"])


class GateReplayEvidenceTests(unittest.TestCase):
    def test_inconsistent_counts_fail_closed(self):
        experiment = gate_replay.load_experiment("R1")
        experiment["paired"]["candidate_correct"] = 30
        with self.assertRaises(AssertionError):
            gate_replay.metrics_from(experiment)

    def test_inconsistent_interval_fails_closed(self):
        experiment = gate_replay.load_experiment("R2")
        experiment["statistics"]["bootstrap"]["lower"] = -0.5
        with self.assertRaises(AssertionError):
            gate_replay.metrics_from(experiment)

    def test_script_writes_replay_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "gate_replay.json"
            self.assertEqual(gate_replay.main(["--output", str(target)]), 0)
            report = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(report["schema"], "gate-replay/1")
        self.assertEqual(report["methods"]["bootstrap_seed"], 1)
        self.assertEqual(report["methods"]["resamples"], 2000)
        self.assertEqual(set(report["experiments"]), {"R1", "R2"})
        self.assertEqual(report["experiments"]["R1"]["decisions"]["screen"]["passed"], True)
        self.assertEqual(report["experiments"]["R2"]["decisions"]["release"]["passed"], False)
        self.assertEqual(len(report["experiments"]["R2"]["decisions"]["release"]["reasons"]), 4)


if __name__ == "__main__":
    unittest.main()
