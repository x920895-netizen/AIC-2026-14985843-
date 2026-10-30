"""两级门禁的纯函数行为与验证器集成。"""

import copy
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("DSPY_CACHEDIR", str(ROOT / ".cache/dspy"))

from harness import CandidateProposal, EvolutionFoundation, FoundationStore, GateSpec, RuntimeTask, TargetKind
from harness.gates import (CUSTOM, RELEASE, RELEASE_THRESHOLDS, SCREEN, SCREEN_THRESHOLDS,
                           build_spec, cost_ratio, evaluate_gate, gate_profile, spec_with_profile)


def metrics(**overrides):
    base = dict(delta_em=0.05, delta_f1=0.02, regression_rate=0.0, interval=[0.01, 0.10],
                parent_cost_cny=1.0, candidate_cost_cny=1.0)
    base.update(overrides)
    return base


class GatePresetTests(unittest.TestCase):
    def spec(self, **overrides):
        return GateSpec.release("ds", ("q1", "q2"), {"model": "offline"}, **overrides)

    def test_screen_and_release_presets_are_fixed_values(self):
        self.assertEqual(SCREEN_THRESHOLDS, dict(min_em_gain=0.02, max_f1_drop=0.02,
                                                max_regression_rate=0.10, max_cost_ratio=2.0,
                                                require_positive_interval=False))
        self.assertEqual(RELEASE_THRESHOLDS, dict(min_em_gain=0.02, max_f1_drop=0.01,
                                                 max_regression_rate=0.05, max_cost_ratio=1.2,
                                                 require_positive_interval=True))
        screen = GateSpec.screen("ds", ("q1",), {"model": "offline"})
        release = GateSpec.release("ds", ("q1",), {"model": "offline"})
        self.assertFalse(screen.require_positive_interval)
        self.assertTrue(release.require_positive_interval)
        self.assertGreater(screen.max_cost_ratio, release.max_cost_ratio)

    def test_default_construction_is_still_release(self):
        default = GateSpec("ds", ("q1", "q2"), {"model": "offline"})
        self.assertEqual(default.to_dict(), self.spec().to_dict())
        self.assertEqual(gate_profile(default), RELEASE)
        self.assertEqual(gate_profile(self.spec(max_cost_ratio=1.5)), CUSTOM)

    def test_preset_override_and_unknown_parameter(self):
        spec = GateSpec.screen("ds", ("q1",), {"model": "offline"}, max_cost_ratio=3.0)
        self.assertEqual(spec.max_cost_ratio, 3.0)
        self.assertEqual(gate_profile(spec), CUSTOM)
        with self.assertRaises(ValueError):
            build_spec("unknown", "ds", ("q1",), {})
        with self.assertRaises(ValueError):
            build_spec(SCREEN, "ds", ("q1",), {}, not_a_gate=1)

    def test_spec_with_profile_switches_thresholds_only(self):
        spec = self.spec()
        screen = spec_with_profile(spec, SCREEN)
        self.assertEqual(screen.dataset_id, spec.dataset_id)
        self.assertEqual(screen.task_ids, spec.task_ids)
        self.assertEqual(gate_profile(screen), SCREEN)


class EvaluateGateTests(unittest.TestCase):
    def setUp(self):
        self.release = GateSpec.release("ds", ("q1",), {"model": "offline"})
        self.screen = GateSpec.screen("ds", ("q1",), {"model": "offline"})

    def test_release_rejects_each_condition_independently(self):
        cases = [
            (metrics(delta_em=0.0), "主指标EM增益不足"),
            (metrics(delta_f1=-0.02), "辅助指标F1退化超限"),
            (metrics(regression_rate=0.10), "逐题回归率超限"),
            (metrics(candidate_cost_cny=1.3), "候选推理成本比超限"),
            (metrics(interval=[0.0, 0.2]), "配对区间不足以支持正增益"),
        ]
        for case, reason in cases:
            with self.subTest(reason=reason):
                passed, reasons = evaluate_gate(case, self.release)
                self.assertFalse(passed)
                self.assertEqual(reasons, [reason])

    def test_screen_tolerates_cost_and_interval_but_not_em_loss(self):
        lenient = metrics(delta_em=0.0, candidate_cost_cny=1.9, interval=[-0.05, 0.2])
        passed, reasons = evaluate_gate(lenient, self.screen)
        self.assertFalse(passed)
        self.assertEqual(reasons, ["主指标EM增益不足"])

        allowed = metrics(delta_em=0.03, candidate_cost_cny=1.9, interval=[-0.05, 0.2],
                          regression_rate=0.09)
        passed, reasons = evaluate_gate(allowed, self.screen)
        self.assertTrue(passed, reasons)

        blocked = metrics(delta_em=0.03, candidate_cost_cny=2.5, interval=[0.01, 0.2])
        passed, reasons = evaluate_gate(blocked, self.screen)
        self.assertEqual(reasons, ["候选推理成本比超限"])

    def test_release_reports_all_vetoes_in_stable_order(self):
        worst = metrics(delta_em=-0.1, delta_f1=-0.2, regression_rate=0.5,
                        candidate_cost_cny=3.0, interval=[-0.3, -0.1])
        passed, reasons = evaluate_gate(worst, self.release)
        self.assertFalse(passed)
        self.assertEqual(reasons, ["主指标EM增益不足", "辅助指标F1退化超限", "逐题回归率超限",
                                   "候选推理成本比超限", "配对区间不足以支持正增益"])

    def test_evaluate_gate_is_pure_and_does_not_mutate_metrics(self):
        value = metrics(candidate_cost_cny=2.0)
        snapshot = copy.deepcopy(value)
        first = evaluate_gate(value, self.release)
        second = evaluate_gate(value, self.release)
        self.assertEqual(value, snapshot)
        self.assertEqual(first, second)
        first[1].append("外部修改")
        self.assertNotIn("外部修改", evaluate_gate(value, self.release)[1])

    def test_cost_ratio_prefers_explicit_value_and_handles_zero_parent(self):
        self.assertEqual(cost_ratio(metrics()), 1.0)
        self.assertEqual(cost_ratio(dict(parent_cost_cny=2.0, candidate_cost_cny=3.0)), 1.5)
        self.assertEqual(cost_ratio(dict(parent_cost_cny=0.0, candidate_cost_cny=0.0)), 1.0)
        self.assertEqual(cost_ratio(dict(parent_cost_cny=0.0, candidate_cost_cny=0.1)), float("inf"))
        with self.assertRaises(ValueError):
            cost_ratio(dict(delta_em=0.1))


class ValidatorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gate-unit-")
        self.addCleanup(self.temp.cleanup)
        self.store = FoundationStore(self.temp.name)
        self.engine = EvolutionFoundation(self.store)
        artifacts = {
            "prompt": {"components": {"final_answer.predict": "初始指令"}},
            "memory": {"experience_ids": [], "retrieval_policy": {}, "lifecycle_policy": {}},
            "tool": {"tools": [], "selection_policy": {}, "risk_constraints": {}},
            "skill": {"definitions": [], "selection_policy": {}, "lifecycle_policy": {}},
        }
        refs = {k: self.store.write_artifact(k, v) for k, v in artifacts.items()}
        self.config = {"model_config": {"model": "offline-deterministic", "temperature": 0}}
        self.base = self.store.initialize(refs, self.config)
        self.tasks = [RuntimeTask(f"gate-{i}", f"问题{i}") for i in range(4)]
        self.labels = {t.task_id: f"answer {i}" for i, t in enumerate(self.tasks)}
        self.engine.register_dataset("gate-set", self.tasks, self.labels)

    def candidate(self, spec, suffix="screen"):
        parent = self.store.current()
        ref = self.store.write_artifact("prompt", {"components": {"final_answer.predict": suffix}})
        return self.engine.register(CandidateProposal(parent["version_id"], TargetKind.PROMPT, ref,
                                                      spec, "门禁集成测试候选"))

    def test_validator_uses_shared_gate_for_screen_profile(self):
        spec = GateSpec.screen("gate-set", tuple(t.task_id for t in self.tasks), self.config["model_config"])
        candidate = self.candidate(spec)
        scope = self.engine.budget.create(1.0)
        old = self.engine.run(candidate["parent_version_id"], spec, scope["scope_id"],
                              lambda task, *_: "incorrect", projected_cost_per_task=0.01)
        new = self.engine.run(candidate["version_id"], spec, scope["scope_id"],
                              lambda task, *_: self.labels[task.task_id], candidate_id=candidate["candidate_id"],
                              projected_cost_per_task=0.01)
        report = self.engine.validate(candidate["candidate_id"], old["run_id"], new["run_id"])
        self.assertEqual(gate_profile(spec), SCREEN)
        expected_passed, expected_reasons = evaluate_gate(report["metrics"], spec)
        self.assertEqual((report["passed"], report["reasons"]), (expected_passed, expected_reasons))
        self.assertTrue(report["passed"])
        self.assertIn("cost_ratio", report["metrics"])

    def test_screen_spec_still_vetoes_regression_above_screen_limit(self):
        spec = GateSpec.screen("gate-set", tuple(t.task_id for t in self.tasks), self.config["model_config"])
        candidate = self.candidate(spec, suffix="regress")
        scope = self.engine.budget.create(1.0)
        old = self.engine.run(candidate["parent_version_id"], spec, scope["scope_id"],
                              lambda task, *_: self.labels[task.task_id], projected_cost_per_task=0.01)
        new = self.engine.run(candidate["version_id"], spec, scope["scope_id"],
                              lambda task, *_: "incorrect", candidate_id=candidate["candidate_id"],
                              projected_cost_per_task=0.01)
        report = self.engine.validate(candidate["candidate_id"], old["run_id"], new["run_id"])
        self.assertFalse(report["passed"])
        self.assertIn("逐题回归率超限", report["reasons"])
        self.assertEqual(report["reasons"], evaluate_gate(report["metrics"], spec)[1])


if __name__ == "__main__":
    unittest.main()
