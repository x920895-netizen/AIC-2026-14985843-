"""协议：导入导出、协议哈希冻结、测试集只跑一次硬约束、凭证绑定 protocol_hash。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import (EvolutionFoundation, FoundationStore, ProtocolSpec, RuntimeTask,
                     build_protocol, protocol_warnings)
from harness.gates import RELEASE, SCREEN
from harness.protocol import TestSetGuard as Guard
from harness.proposers.mock import ScriptedProposer
from harness.runners.mock import MockRunner
from harness.storage import StateConflict

VAL = [f"v{i}" for i in range(1, 7)]
TEST = [f"t{i}" for i in range(1, 5)]
LABELS = {**{t: f"answer {t}" for t in VAL}, **{t: f"answer {t}" for t in TEST}}
SOLVED = {"base": {"v1", "v2", "t1"}, "good": set(VAL) | set(TEST)}


def responder(task, prompt, artifacts):
    solved = SOLVED["good"] if "先逐跳确定" in prompt else SOLVED["base"]
    return LABELS[task.task_id] if task.task_id in solved else "wrong"


class ProtocolSpecTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="protocol-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.protocol = build_protocol("ds-1", VAL, TEST, gate_profile=SCREEN,
                                       min_sample=10, protocol_id="proto-1")

    def test_builds_frozen_protocol_with_expected_fields(self):
        self.assertEqual(self.protocol.schema if hasattr(self.protocol, "schema") else "protocol/1",
                         "protocol/1")
        self.assertEqual(self.protocol.val_task_ids, tuple(VAL))
        self.assertEqual(self.protocol.test_task_ids, tuple(TEST))
        self.assertEqual(self.protocol.gate_profile, SCREEN)
        self.assertEqual(self.protocol.seed, 1)
        self.assertEqual(self.protocol.bootstrap_resamples, 2000)
        self.assertEqual(self.protocol.test_policy, "once")
        self.assertEqual(len(self.protocol.hash), 64)

    def test_hash_ignores_created_at_but_not_content(self):
        same = self.protocol.updated(created_at="2000-01-01T00:00:00+00:00")
        self.assertEqual(same.hash, self.protocol.hash, "同协议跨导出必须得到同一哈希")
        changed = self.protocol.updated(seed=7)
        self.assertNotEqual(changed.hash, self.protocol.hash)
        changed_profile = self.protocol.updated(gate_profile=RELEASE)
        self.assertNotEqual(changed_profile.hash, self.protocol.hash)

    def test_json_round_trip_preserves_hash(self):
        text = self.protocol.to_json()
        restored = ProtocolSpec.from_json(text)
        self.assertEqual(restored.to_dict(), self.protocol.to_dict())
        self.assertEqual(restored.hash, self.protocol.hash)
        again = ProtocolSpec.from_dict(json.loads(text))
        self.assertEqual(again.hash, self.protocol.hash)

    def test_save_and_load_from_disk(self):
        target = self.root/"protocols"/"p.json"
        self.protocol.save(target)
        self.assertTrue(target.is_file())
        loaded = ProtocolSpec.load(target)
        self.assertEqual(loaded.hash, self.protocol.hash)
        self.assertEqual(loaded.val_task_ids, self.protocol.val_task_ids)

    def test_import_rejects_unknown_fields_and_schema(self):
        payload = json.loads(self.protocol.to_json())
        payload["extra"] = 1
        with self.assertRaises(ValueError):
            ProtocolSpec.from_dict(payload)
        payload.pop("extra")
        payload["schema"] = "protocol/9"
        with self.assertRaises(ValueError):
            ProtocolSpec.from_dict(payload)
        with self.assertRaises(ValueError):
            ProtocolSpec.from_dict([])

    def test_overlapping_split_is_refused(self):
        with self.assertRaises(ValueError):
            build_protocol("ds-1", ["a", "b"], ["b", "c"])
        with self.assertRaises(ValueError):
            build_protocol("ds-1", [], ["t1"])

    def test_unknown_evaluator_is_refused(self):
        with self.assertRaises(ValueError):
            build_protocol("ds-1", VAL, TEST, evaluator_id="not-a-scorer")

    def test_gate_spec_carries_frozen_statistics(self):
        spec = self.protocol.gate_spec({"model": "m"}, budget=2.0)
        self.assertEqual(spec.task_ids, tuple(VAL))
        self.assertEqual(spec.seed, 1)
        self.assertEqual(spec.bootstrap_resamples, 2000)
        self.assertEqual(spec.min_sample, 10)
        self.assertEqual(spec.protocol_id, "proto-1")
        self.assertAlmostEqual(spec.max_total_cost_cny, 2.0)
        self.assertFalse(spec.require_offline)
        self.assertFalse(spec.require_positive_interval, "screen 档不应要求正区间")
        test_spec = self.protocol.test_spec({"model": "m"})
        self.assertEqual(test_spec.task_ids, tuple(TEST))

    def test_warnings_flag_small_test_set_and_repeats(self):
        warnings = protocol_warnings(self.protocol)
        self.assertTrue(any("min_sample" in warning for warning in warnings))
        repeated = self.protocol.updated(repeats=3)
        self.assertTrue(any("重复 3 次" in warning for warning in protocol_warnings(repeated)))
        report_only = self.protocol.updated(test_policy="report-only")
        self.assertTrue(any("report-only" in warning for warning in protocol_warnings(report_only)))

    def test_clean_protocol_reports_pass_message(self):
        clean = build_protocol("ds-1", VAL, TEST, min_sample=2)
        self.assertEqual(protocol_warnings(clean),
                         ["协议检查通过：划分互斥、评价器可复算、种子与阈值已冻结"])

    def test_from_gate_spec_recovers_profile(self):
        spec = self.protocol.gate_spec({"model": "m"})
        recovered = ProtocolSpec.from_gate_spec(spec, test_task_ids=TEST)
        self.assertEqual(recovered.gate_profile, SCREEN)
        self.assertEqual(recovered.thresholds["max_cost_ratio"], 2.0)
        self.assertEqual(recovered.test_task_ids, tuple(TEST))


class TestSetGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="guard-")
        self.addCleanup(self.temp.cleanup)
        self.store = FoundationStore(Path(self.temp.name)/"state")
        self.protocol = build_protocol("ds-1", VAL, TEST, min_sample=10)

    def add_run(self, run_id, task_ids, dataset_id="ds-1", status="completed"):
        run = dict(run_id=run_id, status=status, validation_config=dict(
            dataset_id=dataset_id, task_ids=list(task_ids)))
        with self.store.transaction("test.seed") as conn:
            self.store.put(conn, "runs", run_id, run)

    def test_first_run_is_allowed(self):
        result = Guard(self.store).check(self.protocol)
        self.assertFalse(result["reused"])
        self.assertEqual(result["matching_runs"], 0)
        self.assertEqual(result["protocol_hash"], self.protocol.hash)

    def test_second_run_on_same_test_set_is_refused(self):
        self.add_run("run-a", TEST)
        guard = Guard(self.store)
        with self.assertRaises(StateConflict) as caught:
            guard.check(self.protocol)
        self.assertIn("测试集已被其它运行使用过", str(caught.exception))
        self.assertIn("once", str(caught.exception))

    def test_own_runs_are_not_counted(self):
        self.add_run("run-own", TEST)
        result = Guard(self.store).check(self.protocol, own_run_ids=["run-own"])
        self.assertEqual(result["matching_runs"], 0)
        self.assertEqual(result["own_runs"], 1)

    def test_different_dataset_or_split_does_not_match(self):
        self.add_run("run-other-dataset", TEST, dataset_id="ds-2")
        self.add_run("run-other-split", VAL)
        result = Guard(self.store).check(self.protocol)
        self.assertEqual(result["matching_runs"], 0)

    def test_report_only_policy_records_reuse_instead_of_blocking(self):
        self.add_run("run-a", TEST)
        protocol = self.protocol.updated(test_policy="report-only")
        result = Guard(self.store).check(protocol)
        self.assertEqual(result["matching_runs"], 1)
        self.assertEqual(result["policy"], "report-only")

    def test_running_foreign_run_also_blocks(self):
        self.add_run("run-live", TEST, status="running")
        with self.assertRaises(StateConflict):
            Guard(self.store).check(self.protocol)


class EvolveProtocolIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="protocol-evolve-")
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
        self.engine.register_dataset("ds-1", [RuntimeTask(t, f"q {t}") for t in VAL+TEST], LABELS)
        self.runner = MockRunner(responder, workers=4, unit_cost_cny=0.001)

    def evolve(self, protocol, steps=("good_patch", None)):
        return self.engine.evolve("ds-1", VAL, TEST, ScriptedProposer(self.store, list(steps)),
                                  self.runner, gate_profile=SCREEN, max_rounds=2,
                                  budget_cny=5.0, protocol=protocol)

    def test_every_receipt_records_the_protocol_hash(self):
        protocol = build_protocol("ds-1", VAL, TEST, gate_profile=SCREEN, min_sample=10)
        result = self.evolve(protocol)
        self.assertTrue(result["receipts"])
        for receipt in result["receipts"]:
            self.assertEqual(receipt["protocol_hash"], protocol.hash)
        self.assertEqual(result["protocol_hash"], protocol.hash)

    def test_second_evolve_on_same_test_set_is_refused(self):
        protocol = build_protocol("ds-1", VAL, TEST, gate_profile=SCREEN, min_sample=10)
        first = self.evolve(protocol)
        self.assertEqual(first["status"], "completed")
        # 第二次演化即使完全不提议候选（无选择压力），也不得再跑同一测试集。
        with self.assertRaises(StateConflict) as caught:
            self.evolve(protocol, steps=(None,))
        self.assertIn("测试集已被其它运行使用过", str(caught.exception))

    def test_protocol_mismatch_with_evolve_arguments_is_refused(self):
        protocol = build_protocol("ds-1", list(reversed(VAL)), TEST, gate_profile=SCREEN,
                                  min_sample=10)
        with self.assertRaises(ValueError):
            self.evolve(protocol)


if __name__ == "__main__":
    unittest.main()
