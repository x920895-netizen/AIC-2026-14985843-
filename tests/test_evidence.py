"""门禁第三态：样本不足时输出“证据不足”，而不是把噪声当成结论。"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import EvolutionFoundation, FoundationStore, RuntimeTask
from harness.gates import (RELEASE, RELEASE_THRESHOLDS, SCREEN, SCREEN_THRESHOLDS,
                           assess_gate, build_spec, evaluate_gate)
from harness.proposers.mock import ScriptedProposer
from harness.receipt import verify_chain
from harness.runners.mock import MockRunner


def spec(profile=RELEASE, task_ids=None, **overrides):
    thresholds = dict(RELEASE_THRESHOLDS if profile == RELEASE else SCREEN_THRESHOLDS)
    thresholds.update(overrides)
    return build_spec(profile, "ds", task_ids or tuple(f"q{i}" for i in range(1, 41)),
                      {"model": "m"}, require_offline=True, **thresholds)


def metrics(**overrides):
    base = dict(delta_em=0.10, delta_f1=0.05, regression_rate=0.0, cost_ratio=1.0,
                interval=[0.03, 0.20], count=100, parent_em=0.5, discordant=12)
    base.update(overrides)
    return base


class AssessGateTests(unittest.TestCase):
    def test_pass_when_every_criterion_holds(self):
        result = assess_gate(metrics(), spec())
        self.assertEqual(result["verdict"], "pass")
        self.assertTrue(result["passed"])
        self.assertEqual(result["reasons"], [])
        self.assertFalse(result["evidence"]["underpowered"])
        self.assertEqual(result["evidence"]["hard_vetoes"], [])

    def test_small_sample_with_positive_delta_is_insufficient(self):
        # 6 题、+33pp，但配对区间下界仍为 0：结构上没毛病，统计上撑不起结论。
        result = assess_gate(metrics(delta_em=0.3333, delta_f1=0.1, interval=[0.0, 0.6667],
                                     count=6, discordant=6),
                             spec(RELEASE, task_ids=tuple(f"v{i}" for i in range(1, 7))))
        self.assertEqual(result["verdict"], "insufficient")
        self.assertFalse(result["passed"], "证据不足不能算通过")
        self.assertIn("配对区间不足以支持正增益", result["reasons"])
        evidence = result["evidence"]
        self.assertTrue(evidence["underpowered"])
        self.assertEqual(evidence["count"], 6)
        self.assertEqual(evidence["min_sample"], 30)
        self.assertIsNotNone(evidence["mde"])
        self.assertTrue(evidence["below_mde"])
        self.assertTrue(evidence["statistical_vetoes"])
        self.assertEqual(evidence["hard_vetoes"], [])
        self.assertTrue(any("样本不足" in note for note in evidence["notes"]))

    def test_hard_veto_cannot_be_whitewashed_by_small_sample(self):
        result = assess_gate(metrics(delta_em=0.2, interval=[0.0, 0.4], cost_ratio=3.0,
                                     count=6, discordant=4),
                             spec(task_ids=tuple(f"v{i}" for i in range(1, 7))))
        self.assertEqual(result["verdict"], "fail")
        self.assertIn("候选推理成本比超限", result["evidence"]["hard_vetoes"])

    def test_large_sample_with_negative_interval_is_fail(self):
        result = assess_gate(metrics(interval=[-0.01, 0.2], count=200), spec())
        self.assertEqual(result["verdict"], "fail", "样本充足时不能再用“证据不足”兜底")

    def test_negative_delta_is_fail_even_when_underpowered(self):
        result = assess_gate(metrics(delta_em=-0.1, interval=[-0.3, 0.0], count=6, discordant=3),
                             spec(task_ids=tuple(f"v{i}" for i in range(1, 7))))
        self.assertEqual(result["verdict"], "fail")

    def test_f1_and_regression_vetoes_are_hard(self):
        f1 = assess_gate(metrics(delta_f1=-0.5, count=6, discordant=0), spec())
        self.assertEqual(f1["verdict"], "fail")
        self.assertIn("辅助指标F1退化超限", f1["evidence"]["hard_vetoes"])
        regression = assess_gate(metrics(regression_rate=0.5, count=6, discordant=0), spec())
        self.assertEqual(regression["verdict"], "fail")
        self.assertIn("逐题回归率超限", regression["evidence"]["hard_vetoes"])

    def test_min_sample_can_be_overridden_per_call(self):
        underpowered = assess_gate(metrics(interval=[0.0, 0.2], count=12), spec(), min_sample=30)
        powered = assess_gate(metrics(interval=[0.0, 0.2], count=12), spec(), min_sample=10)
        self.assertEqual(underpowered["verdict"], "insufficient")
        self.assertEqual(powered["verdict"], "fail")
        self.assertEqual(powered["evidence"]["min_sample"], 10)

    def test_evidence_reports_mde_and_observed_effect(self):
        result = assess_gate(metrics(delta_em=0.03, interval=[0.005, 0.06], count=100,
                                     discordant=20), spec())
        evidence = result["evidence"]
        self.assertAlmostEqual(evidence["observed_delta"], 0.03, places=12)
        self.assertAlmostEqual(evidence["observed_abs_delta"], 0.03, places=12)
        self.assertAlmostEqual(evidence["mde_discordance"], 0.2, places=12)
        self.assertGreater(evidence["mde"], 0.0)

    def test_min_sample_must_be_positive(self):
        with self.assertRaises(ValueError):
            assess_gate(metrics(), spec(), min_sample=0)

    def test_evaluate_gate_keeps_two_state_contract(self):
        passed, reasons = evaluate_gate(metrics(), spec())
        self.assertIsInstance(passed, bool)
        self.assertIsInstance(reasons, list)
        verdict = assess_gate(metrics(), spec())
        self.assertEqual(passed, verdict["passed"])
        self.assertEqual(reasons, verdict["reasons"])

    def test_insufficient_never_reports_passed(self):
        result = assess_gate(metrics(delta_em=0.3, interval=[0.0, 0.5], count=5, discordant=5),
                             spec(task_ids=tuple(f"v{i}" for i in range(1, 6))))
        self.assertEqual(result["verdict"], "insufficient")
        self.assertFalse(result["passed"])


VAL = [f"v{i}" for i in range(1, 7)]
TEST = ["t1", "t2"]
LABELS = {t: f"answer {t}" for t in VAL+TEST}
SOLVED = {"base": {"v1", "v2", "v3", "t1"}, "good": {"v1", "v2", "v3", "v4", "v5", "t1", "t2"}}


def responder(task, prompt, artifacts):
    solved = SOLVED["good"] if "先逐跳确定" in prompt else SOLVED["base"]
    return LABELS[task.task_id] if task.task_id in solved else "wrong"


class EvolveInsufficientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="insufficient-")
        self.addCleanup(self.temp.cleanup)
        self.store = FoundationStore(Path(self.temp.name)/"state")
        self.engine = EvolutionFoundation(self.store)
        artifacts = {
            "prompt": {"components": {"final_answer.predict": "baseline"}},
            "memory": {"experience_ids": [], "retrieval_policy": {}, "lifecycle_policy": {}},
            "tool": {"tools": [], "selection_policy": {}, "risk_constraints": {}},
            "skill": {"definitions": [], "selection_policy": {}, "lifecycle_policy": {}},
        }
        refs = {kind: self.store.write_artifact(kind, value) for kind, value in artifacts.items()}
        self.store.initialize(refs, {"model_config": {"model": "mock", "temperature": 0}})
        self.engine.register_dataset("ds", [RuntimeTask(t, f"q {t}") for t in VAL+TEST], LABELS)

    def test_six_task_validation_is_recorded_as_insufficient_not_accepted(self):
        result = self.engine.evolve("ds", VAL, TEST,
                                    ScriptedProposer(self.store, ["good_patch", None]),
                                    MockRunner(responder, workers=2, unit_cost_cny=0.001),
                                    gate_profile=RELEASE, max_rounds=2, budget_cny=5.0)
        row = result["rounds"][0]
        self.assertEqual(row["decision"], "insufficient")
        self.assertEqual(row["verdict"], "insufficient")
        self.assertIn(result["rounds"][0]["candidate_id"], result["insufficient"])
        self.assertEqual(result["accepted"], [])
        self.assertEqual(result["final_version"], result["base_version"],
                         "证据不足不能推进版本")
        self.assertEqual(result["receipts"][0]["decision"], "insufficient")
        with self.store.connection() as conn:
            self.assertTrue(verify_chain(conn)["verified"])
        report = self.store.read("evaluations", self.store.read(
            "candidates", result["rounds"][0]["candidate_id"])["evaluation_ids"][-1])
        self.assertEqual(report["verdict"], "insufficient")
        self.assertTrue(report["evidence"]["underpowered"])
        self.assertFalse(report["passed"])

    def test_stats_view_surfaces_underpowered_flag(self):
        result = self.engine.evolve("ds", VAL, TEST,
                                    ScriptedProposer(self.store, ["good_patch", None]),
                                    MockRunner(responder, workers=2, unit_cost_cny=0.001),
                                    gate_profile=RELEASE, max_rounds=2, budget_cny=5.0)
        view = self.engine.stats(result["run_id"])
        self.assertEqual(view["rounds"][0]["verdict"], "insufficient")
        self.assertTrue(view["rounds"][0]["underpowered"])
        self.assertIsNotNone(view["rounds"][0]["mde"])
        self.assertEqual(view["selection"]["distinct_candidates"], 1)


if __name__ == "__main__":
    unittest.main()
