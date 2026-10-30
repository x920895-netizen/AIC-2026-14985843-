"""在线闭环：3 轮内 1 拒 1 收、测试集不参与选择、凭证链完整、可断点续跑。"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import EvolutionFoundation, FoundationStore, RuntimeTask
from harness.proposers.mock import ScriptedProposer
from harness.receipt import verify_chain
from harness.runners.mock import MockRunner

VAL = [f"v{i}" for i in range(1, 9)]
TEST = [f"t{i}" for i in range(1, 5)]
LABELS = {**{t: f"answer {t}" for t in VAL}, **{t: f"answer {t}" for t in TEST}}
SOLVED = {
    "base": {"v1", "v2", "v3", "v4", "t1", "t2"},
    "bad": {"v1", "v2", "t1"},
    "good": set(VAL) | set(TEST),
}


def variant(prompt: str) -> str:
    if "先逐跳确定" in prompt:
        return "good"
    if "回答尽量简短" in prompt:
        return "bad"
    return "base"


def responder(task, prompt, artifacts):
    return LABELS[task.task_id] if task.task_id in SOLVED[variant(prompt)] else "wrong"


class EvolveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="evolve-")
        self.addCleanup(self.temp.cleanup)
        self.store = FoundationStore(self.temp.name)
        self.engine = EvolutionFoundation(self.store)
        artifacts = {
            "prompt": {"components": {"final_answer.predict": "baseline"}},
            "memory": {"experience_ids": [], "retrieval_policy": {}, "lifecycle_policy": {}},
            "tool": {"tools": [], "selection_policy": {}, "risk_constraints": {}},
            "skill": {"definitions": [], "selection_policy": {}, "lifecycle_policy": {}},
        }
        refs = {k: self.store.write_artifact(k, v) for k, v in artifacts.items()}
        self.base = self.store.initialize(refs, {"model_config": {"model": "mock-deterministic", "temperature": 0}})
        tasks = [RuntimeTask(t, f"question {t}") for t in VAL+TEST]
        self.engine.register_dataset("evolve-set", tasks, LABELS)
        self.runner = MockRunner(responder, workers=4, unit_cost_cny=0.001)

    def evolve(self, steps=("bad_patch", "good_patch", None), **kwargs):
        proposer = ScriptedProposer(self.store, list(steps))
        params = dict(gate_profile="release", max_rounds=3, budget_cny=5.0)
        params.update(kwargs)
        return self.engine.evolve("evolve-set", VAL, TEST, proposer, self.runner, **params)

    def test_three_rounds_with_one_reject_and_one_accept(self):
        result = self.evolve()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["stop_reason"], "proposer_declined")
        self.assertEqual([r["decision"] for r in result["rounds"]], ["reject", "accept", "stop"])
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual(len(result["rejected"]), 1)
        first, second = result["rounds"][0], result["rounds"][1]
        self.assertIn("主指标EM增益不足", first["reasons"])
        self.assertEqual(second["reasons"], [])
        self.assertLess(first["metrics"]["delta_em"], 0)
        self.assertGreater(second["metrics"]["delta_em"], 0)
        final_prompt = self.store.load_artifact(self.store.version(result["final_version"])["artifacts"]["prompt"])
        self.assertIn("先逐跳确定", final_prompt["components"]["final_answer.predict"])
        self.assertNotEqual(result["final_version"], result["base_version"])

    def test_test_set_never_used_for_selection(self):
        result = self.evolve()
        val_run_ids = {row["run_id"] for row in result["rounds"] if row["run_id"]}
        test = result["test_metrics"]
        self.assertFalse(test["selection_used_test"])
        self.assertNotIn(test["baseline_run_id"], val_run_ids)
        self.assertNotIn(test["candidate_run_id"], val_run_ids)
        for candidate_id in result["accepted"]+result["rejected"]:
            candidate = self.store.read("candidates", candidate_id)
            self.assertEqual(tuple(candidate["validation_config"]["task_ids"]), tuple(VAL))
            self.assertNotIn("t1", candidate["validation_config"]["task_ids"])
        self.assertGreater(test["candidate"]["em"], test["baseline"]["em"])

    def test_receipts_form_a_verified_chain(self):
        result = self.evolve()
        self.assertEqual([r["decision"] for r in result["receipts"]], ["reject", "accept"])
        self.assertEqual(result["receipts"][1]["prev_receipt_hash"], result["receipts"][0]["receipt_hash"])
        with self.store.connection() as conn:
            chain = verify_chain(conn)
        self.assertTrue(chain["verified"])
        self.assertEqual(chain["count"], 2)
        self.assertEqual({r["candidate_id"] for r in result["receipts"]},
                         set(result["accepted"]) | set(result["rejected"]))

    def test_resume_continues_from_completed_round(self):
        calls = {"n": 0}

        def hook(record):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyboardInterrupt("模拟进程中断")

        original = self.engine._after_round
        self.engine._after_round = hook
        self.addCleanup(setattr, self.engine, "_after_round", original)
        with self.assertRaises(KeyboardInterrupt):
            self.evolve()
        self.engine._after_round = original
        record = self.store.list("evolution_runs")[0]
        self.assertEqual(record["round_index"], 1)
        self.assertEqual(record["status"], "running")
        resumed = self.engine.resume(record["run_id"], ScriptedProposer(self.store, ["bad_patch", "good_patch", None]),
                                     self.runner)
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(len(resumed["rounds"]), 3)
        self.assertEqual(len(resumed["accepted"]), 1)
        self.assertEqual(len(resumed["rejected"]), 1)
        self.assertEqual([r["round"] for r in resumed["rounds"]], [1, 2, 3])

    def test_two_consecutive_rejects_stop_early(self):
        result = self.evolve(steps=["bad_patch", "bad_patch", "good_patch", "good_patch"], max_rounds=4)
        self.assertEqual(result["stop_reason"], "no_improvement")
        self.assertEqual(len(result["rounds"]), 2)
        self.assertEqual(result["accepted"], [])
        self.assertEqual(result["final_version"], result["base_version"])

    def test_max_rounds_stop(self):
        result = self.evolve(steps=["bad_patch", "good_patch"], max_rounds=2)
        self.assertEqual(result["stop_reason"], "max_rounds")
        self.assertEqual(len(result["rounds"]), 2)
        self.assertEqual(len(result["accepted"]), 1)

    def test_budget_exhaustion_is_reported(self):
        result = self.evolve(budget_cny=0.0001)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stop_reason"], "baseline_failed")
        self.assertEqual(result["rounds"], [])

    def test_cost_is_simulated_and_api_calls_are_counted(self):
        result = self.evolve()
        self.assertFalse(result["external_api"])
        self.assertGreater(result["cost_cny"], 0)
        self.assertEqual(result["actual_cost_cny"], 0)
        self.assertGreater(result["api_calls"], 0)


if __name__ == "__main__":
    unittest.main()
