"""幂等与恢复：重复 accept 返回既有凭证；5 个轮次内中断点都能续跑到底。"""

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
from harness.storage import StateConflict

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


#: 轮次内的 5 个持久化边界；每个都必须能从中断处续跑。
INTERRUPTION_POINTS = (
    "evolve.before_register",
    "evolve.after_register",
    "evolve.after_run",
    "evolve.after_validate",
    "evolve.after_accept",
)


class HarnessCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="resume-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/"state"
        self.store = FoundationStore(self.root)
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
        self.runner = MockRunner(responder, workers=4, unit_cost_cny=0.001)
        self.steps = ["bad_patch", "good_patch"]

    def evolve(self, engine=None, steps=None, max_rounds=2):
        engine = engine or self.engine
        return engine.evolve("ds", VAL, TEST, ScriptedProposer(self.store, list(steps or self.steps)),
                             self.runner, gate_profile="release", max_rounds=max_rounds,
                             budget_cny=5.0)

    def resume(self, engine=None):
        engine = engine or self.engine
        record = self.store.list("evolution_runs")[0]
        return engine.resume(record["run_id"], ScriptedProposer(self.store, list(self.steps)),
                             self.runner)

    def assert_single_round_result(self, result):
        self.assertEqual(result["status"], "completed")
        self.assertEqual([row["decision"] for row in result["rounds"]], ["reject", "accept"])
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual(len(result["rejected"]), 1)
        self.assertNotEqual(result["final_version"], result["base_version"])
        candidate_ids = [row["candidate_id"] for row in result["rounds"]]
        self.assertEqual(len(candidate_ids), len(set(candidate_ids)), "续跑不得重复登记同一候选")
        run_ids = {row["run_id"] for row in result["rounds"] if row["run_id"]}
        for run_id in run_ids:
            run = self.store.read("runs", run_id)
            keys = [(row["task_id"], row["repeat"]) for row in run["outputs"]]
            self.assertEqual(len(keys), len(set(keys)), f"{run_id} 出现重复任务输出")
            self.assertEqual(len(keys), len(VAL), f"{run_id} 任务数不符")
        with self.store.connection() as conn:
            chain = verify_chain(conn)
        self.assertTrue(chain["verified"])
        self.assertEqual(chain["count"], 2)


class AcceptIdempotencyTests(HarnessCase):
    def test_repeated_accept_returns_existing_receipt(self):
        result = self.evolve()
        accepted = result["accepted"][0]
        first_receipt = [r for r in result["receipts"] if r["decision"] == "accept"][0]
        with self.store.connection() as conn:
            before = len(self.engine.receipts.all(conn))
        before_version = self.store.current()["version_id"]
        again = self.engine.accept(accepted)
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["receipt"]["receipt_id"], first_receipt["receipt_id"])
        self.assertEqual(again["receipt"]["receipt_hash"], first_receipt["receipt_hash"])
        self.assertEqual(self.store.current()["version_id"], before_version,
                         "重复接受不得再次推进指针")
        with self.store.connection() as conn:
            self.assertEqual(len(self.engine.receipts.all(conn)), before)
            self.assertTrue(verify_chain(conn)["verified"])

    def test_idempotency_survives_reopening_the_store(self):
        result = self.evolve()
        accepted = result["accepted"][0]
        first = [r for r in result["receipts"] if r["decision"] == "accept"][0]
        reopened = EvolutionFoundation(FoundationStore(self.root))
        again = reopened.accept(accepted)
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["receipt"]["receipt_id"], first["receipt_id"])

    def test_accept_still_refuses_unvalidated_candidates(self):
        result = self.evolve()
        rejected = result["rejected"][0]
        with self.assertRaises(StateConflict):
            self.engine.accept(rejected)

    def test_accept_requires_receipt_to_be_present(self):
        result = self.evolve()
        accepted = result["accepted"][0]
        with self.store.transaction("tamper") as conn:
            conn.execute("DELETE FROM receipts WHERE candidate_id=? AND decision='accept'", (accepted,))
        with self.assertRaises(StateConflict) as caught:
            self.engine.accept(accepted)
        self.assertIn("缺少 accept 凭证", str(caught.exception))


class IntraRoundResumeTests(HarnessCase):
    def interrupt_at(self, point):
        fired = {"count": 0}

        def hook(name):
            if name == point and fired["count"] == 0:
                fired["count"] += 1
                raise RuntimeError(f"模拟中断：{point}")

        self.store.fault_hook = hook
        self.addCleanup(setattr, self.store, "fault_hook", None)
        with self.assertRaises(RuntimeError):
            self.evolve()
        self.assertEqual(fired["count"], 1, f"中断点 {point} 未被触发")
        self.store.fault_hook = None
        record = self.store.list("evolution_runs")[0]
        self.assertEqual(record["status"], "running", "中断后演化必须仍是 running")
        return record

    def test_resume_before_register(self):
        record = self.interrupt_at("evolve.before_register")
        self.assertEqual(record["round_index"], 0)
        self.assertEqual(record["stage"]["step"], "propose")
        self.assert_single_round_result(self.resume())

    def test_resume_after_register(self):
        record = self.interrupt_at("evolve.after_register")
        self.assertEqual(record["round_index"], 0)
        self.assertEqual(record["stage"]["step"], "run")
        self.assertIsNotNone(record["stage"]["candidate_id"])
        resumed = self.resume()
        self.assert_single_round_result(resumed)
        self.assertEqual(self.store.read("candidates", record["stage"]["candidate_id"])["status"],
                         "rejected", "被中断的那一轮是 bad_patch，续跑后应仍被判拒")

    def test_resume_after_run(self):
        record = self.interrupt_at("evolve.after_run")
        self.assertEqual(record["stage"]["step"], "validate")
        self.assertIsNotNone(record["stage"]["child_run_id"])
        self.assert_single_round_result(self.resume())

    def test_resume_after_validate(self):
        record = self.interrupt_at("evolve.after_validate")
        self.assertEqual(record["stage"]["step"], "decide")
        self.assert_single_round_result(self.resume())

    def test_resume_after_accept(self):
        record = self.interrupt_at("evolve.after_accept")
        self.assertEqual(record["round_index"], 2, "接受落盘后轮次计数必须已推进")
        self.assertIsNone(record["stage"])
        self.assertEqual(len(record["accepted"]), 1, "第 2 轮 good_patch 通过后即中断")
        self.assertEqual(len(record["rejected"]), 1)
        resumed = self.resume()
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(len(resumed["rounds"]), 2)
        self.assertEqual(len({r["candidate_id"] for r in resumed["rounds"]}), 2)

    def test_every_interruption_point_resumes_to_the_same_decisions(self):
        baseline = [row["decision"] for row in self.evolve()["rounds"]]
        self.assertEqual(baseline, ["reject", "accept"])
        for point in INTERRUPTION_POINTS:
            with self.subTest(point=point):
                self.tearDown_store()
                self.setUp()
                record = self.interrupt_at(point)
                resumed = self.resume()
                self.assertEqual([row["decision"] for row in resumed["rounds"]], baseline)
                self.assertEqual(resumed["run_id"], record["run_id"])

    def tearDown_store(self):
        self.temp.cleanup()


class TestPhaseResumeTests(HarnessCase):
    def test_test_phase_resumes_without_rerunning_the_same_run(self):
        original = self.engine._run_test_version
        fired = {"count": 0}

        def patched(record, spec, runner, executor, phase, key, version_id):
            result = original(record, spec, runner, executor, phase, key, version_id)
            if key == "base_run_id" and fired["count"] == 0:
                fired["count"] += 1
                raise RuntimeError("模拟测试阶段中断")
            return result

        self.engine._run_test_version = patched
        self.addCleanup(setattr, self.engine, "_run_test_version", original)
        with self.assertRaises(RuntimeError):
            self.evolve()
        record = self.store.list("evolution_runs")[0]
        self.assertEqual(record["stage"] is None, True, "轮次已全部提交")
        self.assertEqual(record["test_phase"]["status"], "running")
        base_run_id = record["test_phase"]["base_run_id"]
        self.assertIsNotNone(base_run_id)
        self.engine._run_test_version = original

        resumed = self.resume()
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["test_metrics"]["baseline_run_id"], base_run_id,
                         "测试阶段续跑应复用同一个基线运行")
        self.assertFalse(resumed["test_metrics"]["selection_used_test"])
        self.assertEqual(resumed["test_metrics"]["test_policy"], "once")
        reused = self.store.read("runs", base_run_id)
        self.assertEqual(reused["status"], "completed")
        self.assertIsNone(reused.get("termination_reason"))


if __name__ == "__main__":
    unittest.main()
