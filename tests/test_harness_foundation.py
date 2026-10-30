"""Phase 3-B真实公共控制流的离线验收，预测为明确标记的确定性fixture。"""

import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("DSPY_CACHEDIR", str(ROOT/".cache/dspy"))

from harness import CandidateProposal, EvidenceRef, EvolutionFoundation, FoundationStore, GateSpec, RuntimeTask, TargetKind
from harness.budget import BudgetExceeded, read_legacy_usage
from harness.contracts import content_hash
from harness.evaluation import score
from harness.storage import IntegrityError, StateConflict


class HarnessFoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="harness-offline-")
        self.addCleanup(self.temp.cleanup)
        self.store = FoundationStore(self.temp.name)
        self.foundation = EvolutionFoundation(self.store)
        self.contents = {
            "prompt": {"components": {"final_answer.predict": "旧的通用指令"}},
            "memory": {"experience_ids": [], "retrieval_policy": {"limit": 2}, "lifecycle_policy": {"expire_after": 3}},
            "tool": {"tools": ["bm25.search"], "selection_policy": {"k": 7}, "risk_constraints": {"read_only": True}},
            "skill": {"definitions": [], "selection_policy": {"mode": "default"}, "lifecycle_policy": {"review_after": 2}},
        }
        self.refs = {kind: self.store.write_artifact(kind, value) for kind, value in self.contents.items()}
        self.config = {"model_config": {"model": "offline-deterministic", "temperature": 0}}
        self.base = self.store.initialize(self.refs, self.config, experiment_config={"study_id": "offline-foundation"})
        self.tasks = [RuntimeTask(f"fixture-{i}", f"本地合成问题{i}") for i in range(4)]
        self.labels = {t.task_id: f"answer {i}" for i, t in enumerate(self.tasks)}
        self.foundation.register_dataset("fixture-set", self.tasks, self.labels)
        self.spec = GateSpec("fixture-set", tuple(t.task_id for t in self.tasks), self.config["model_config"])

    def runner(self, *, correct=True, outputs=None):
        predictions = outputs or {t.task_id: self.labels[t.task_id] if correct else "incorrect" for t in self.tasks}

        def execute(task, version, artifacts):
            self.assertIsInstance(task, RuntimeTask)
            self.assertFalse(hasattr(task, "answer"))
            self.assertEqual(set(artifacts), {k.value for k in TargetKind})
            self.assertEqual(artifacts, {k: self.store.load_artifact(ref) for k, ref in version["artifacts"].items()})
            return predictions[task.task_id]
        return execute

    def candidate(self, kind="prompt", *, experience_ids=(), memory_ids=(), spec=None, suffix="new"):
        parent = self.store.current()
        value = copy.deepcopy(self.store.load_artifact(parent["artifacts"][kind]))
        if kind == "prompt":
            value["components"]["final_answer.predict"] = "新的通用指令："+suffix
        elif kind == "memory":
            value["experience_ids"] = list(memory_ids)
            value["retrieval_policy"]["mode"] = suffix
        elif kind == "tool":
            value["selection_policy"]["mode"] = suffix
        else:
            value["selection_policy"]["mode"] = suffix
        ref = self.store.write_artifact(kind, value)
        proposal = CandidateProposal(parent["version_id"], TargetKind(kind), ref, spec or self.spec,
            "合成任务的离线协议验收", experience_ids=tuple(experience_ids),
            expected_benefit="仅用于验证公共门禁", risks=("不代表真实模型效果",))
        return self.foundation.register(proposal)

    def pair(self, candidate, *, parent_outputs=None, child_outputs=None, parent_correct=False,
             child_correct=True, cost=0.001, limit=1.0):
        spec = GateSpec.from_dict(candidate["validation_config"])
        scope = self.foundation.budget.create(limit)
        old = self.foundation.run(candidate["parent_version_id"], spec, scope["scope_id"],
            self.runner(correct=parent_correct, outputs=parent_outputs), projected_cost_per_task=cost)
        new = self.foundation.run(candidate["version_id"], spec, scope["scope_id"],
            self.runner(correct=child_correct, outputs=child_outputs), candidate_id=candidate["candidate_id"],
            projected_cost_per_task=cost)
        return old, new, scope

    def validate_pair(self, candidate, **kwargs):
        old, new, scope = self.pair(candidate, **kwargs)
        report = self.foundation.validate(candidate["candidate_id"], old["run_id"], new["run_id"])
        return report, old, new, scope

    def test_A_accept_creates_verified_version_and_preserves_parent(self):
        bootstrap = self.store.read("decisions", self.base["decision_ids"][0])
        self.assertEqual(bootstrap["mode"], "offline_bootstrap")
        self.assertIsNone(bootstrap["evaluation_id"])
        candidate = self.candidate()
        report, old, new, _ = self.validate_pair(candidate)
        self.assertTrue(report["passed"])
        self.assertEqual(self.store.current()["version_id"], self.base["version_id"])
        accepted = self.foundation.accept(candidate["candidate_id"])
        self.assertEqual(accepted["parent_version_id"], self.base["version_id"])
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(self.store.current()["version_id"], accepted["version_id"])
        self.assertEqual(self.store.version(self.base["version_id"]), self.base)
        decision = self.store.read("decisions", accepted["decision_ids"][-1])
        self.assertEqual(decision["verdict"], "accepted")
        self.assertEqual(self.store.read("evaluations", decision["evaluation_id"])["run_ids"], [old["run_id"], new["run_id"]])
        self.store.rollback(self.base["version_id"], "离线恢复历史版本")
        self.assertEqual(self.store.current()["version_id"], self.base["version_id"])

    def test_B_bad_candidate_is_rejected_with_persistent_reason(self):
        candidate = self.candidate()
        report, _, _, _ = self.validate_pair(candidate, child_correct=False)
        self.assertFalse(report["passed"])
        self.assertIn("主指标EM增益不足", report["reasons"])
        with self.assertRaises(StateConflict):
            self.foundation.accept(candidate["candidate_id"])
        reopened = FoundationStore(self.temp.name)
        self.assertEqual(reopened.current()["version_id"], self.base["version_id"])
        rejected = reopened.read("candidates", candidate["candidate_id"])
        self.assertEqual(rejected["status"], "rejected")
        self.assertTrue(reopened.read("decisions", rejected["decision_ids"][-1])["reasons"])

    def test_B_freeze_scorer_dataset_and_configuration(self):
        with self.assertRaises(ValueError):
            replace(self.spec, evaluator_id="candidate-custom-scorer")
        with self.assertRaises(sqlite3.IntegrityError):
            self.foundation.register_dataset("fixture-set", self.tasks, {t.task_id: "changed" for t in self.tasks})
        candidate = self.candidate()
        scope = self.foundation.budget.create(1)
        with self.assertRaises(StateConflict):
            self.foundation.run(candidate["version_id"], replace(self.spec, min_em_gain=0), scope["scope_id"],
                self.runner(), candidate_id=candidate["candidate_id"])
        self.assertEqual(self.store.list("runs"), [])
        with self.assertRaises(StateConflict):
            self.candidate(spec=replace(self.spec, model_config={"model": "changed"}), suffix="changed")

    def test_B_duplicate_proposal_is_not_retried(self):
        candidate = self.candidate()
        self.validate_pair(candidate, child_correct=False)
        with self.assertRaises(StateConflict):
            self.candidate()

    def test_C_regression_vetoes_partial_metric_improvement(self):
        spec = replace(self.spec, require_positive_interval=False)
        candidate = self.candidate(spec=spec)
        old = {self.tasks[0].task_id: self.labels[self.tasks[0].task_id],
               **{t.task_id: "incorrect" for t in self.tasks[1:]}}
        child = {self.tasks[0].task_id: "incorrect", **{t.task_id: self.labels[t.task_id] for t in self.tasks[1:]}}
        report, _, _, _ = self.validate_pair(candidate, parent_outputs=old, child_outputs=child)
        self.assertGreater(report["metrics"]["delta_em"], 0)
        self.assertGreater(report["metrics"]["delta_f1"], 0)
        self.assertIn("逐题回归率超限", report["reasons"])
        self.assertFalse(report["passed"])
        self.assertEqual(self.store.current()["version_id"], self.base["version_id"])

    def test_C_f1_and_cost_constraints_are_independent_vetoes(self):
        candidate = self.candidate(spec=replace(self.spec, max_cost_ratio=0.5))
        report, _, _, _ = self.validate_pair(candidate)
        self.assertIn("候选推理成本比超限", report["reasons"])
        self.assertFalse(report["passed"])
        # 这组构造令EM上升但多数题的部分词元得分下降。
        tasks = [RuntimeTask(f"partial-{i}", f"合成部分匹配{i}") for i in range(4)]
        labels = {t.task_id: "alpha beta gamma" for t in tasks}
        self.foundation.register_dataset("partial-set", tasks, labels)
        spec = GateSpec("partial-set", tuple(t.task_id for t in tasks), self.config["model_config"],
                        require_positive_interval=False)
        c2 = self.candidate(spec=spec, suffix="f1")
        scope = self.foundation.budget.create(1)
        p = self.foundation.run(self.base["version_id"], spec, scope["scope_id"], lambda *a: "alpha beta")
        q = self.foundation.run(c2["version_id"], spec, scope["scope_id"],
            lambda task,*a: labels[task.task_id] if task.task_id == tasks[0].task_id else "incorrect",
            candidate_id=c2["candidate_id"])
        report = self.foundation.validate(c2["candidate_id"], p["run_id"], q["run_id"])
        self.assertGreater(report["metrics"]["delta_em"], 0)
        self.assertIn("辅助指标F1退化超限", report["reasons"])

    def test_D_evaluation_exception_and_missing_runs_cannot_accept(self):
        candidate = self.candidate()
        old, new, _ = self.pair(candidate)
        with patch("harness.evaluation.score", side_effect=RuntimeError("注入评价故障")):
            report = self.foundation.validate(candidate["candidate_id"], old["run_id"], new["run_id"])
        self.assertFalse(report["passed"])
        self.assertIn("注入评价故障", " ".join(report["reasons"]))
        c2 = self.candidate(suffix="missing")
        report = self.foundation.validate(c2["candidate_id"], "missing-parent-run", "missing-child-run")
        self.assertFalse(report["passed"])
        self.assertEqual(self.store.current()["version_id"], self.base["version_id"])

    def test_D_accept_transaction_rolls_back_at_each_failure_point(self):
        candidate = self.candidate()
        self.validate_pair(candidate)
        for point in ["accept.after_version", "accept.after_pointer", "accept.before_commit"]:
            with self.subTest(point=point):
                def fault(name):
                    if name == point:
                        raise OSError("注入事务持久化故障")
                self.store.fault_hook = fault
                with self.assertRaises(OSError):
                    self.foundation.accept(candidate["candidate_id"])
                reopened = FoundationStore(self.temp.name)
                self.assertEqual(reopened.current()["version_id"], self.base["version_id"])
                self.assertEqual(reopened.version(candidate["version_id"])["status"], "candidate")
                self.assertEqual(reopened.read("candidates", candidate["candidate_id"])["status"], "validated")
                self.assertFalse(any(e["kind"] == "accepted" for e in reopened.events()))
        self.store.fault_hook = None
        self.foundation.accept(candidate["candidate_id"])

    def test_D_atomic_artifact_write_failure_leaves_parent_usable(self):
        with patch("harness.storage.os.replace", side_effect=OSError("模拟文件替换失败")):
            with self.assertRaises(OSError):
                self.store.write_artifact("prompt", {"components": {"x": "new"}})
        self.assertEqual(self.store.current(), self.base)
        self.assertEqual(list((self.store.root/"artifacts").glob("*.tmp")), [])

    def test_D_rollback_failure_keeps_current_and_history(self):
        candidate = self.candidate()
        self.validate_pair(candidate)
        accepted = self.foundation.accept(candidate["candidate_id"])
        def fault(point):
            if point == "rollback.before_commit":
                raise OSError("模拟回滚写盘失败")
        self.store.fault_hook = fault
        with self.assertRaises(OSError):
            self.store.rollback(self.base["version_id"], "离线故障测试")
        self.assertEqual(FoundationStore(self.temp.name).current()["version_id"], accepted["version_id"])
        self.store.fault_hook = None
        self.store.rollback(self.base["version_id"], "重试已知未提交的本地事务")

    def test_D_runner_failure_is_persisted_and_vetoed(self):
        candidate = self.candidate()
        scope = self.foundation.budget.create(1)
        old = self.foundation.run(self.base["version_id"], self.spec, scope["scope_id"], self.runner(correct=False))
        def fail(*args):
            raise RuntimeError("合成执行器故障")
        child = self.foundation.run(candidate["version_id"], self.spec, scope["scope_id"], fail,
                                    candidate_id=candidate["candidate_id"])
        self.assertEqual(child["status"], "failed")
        self.assertIn("合成执行器故障", child["termination_reason"])
        report = self.foundation.validate(candidate["candidate_id"], old["run_id"], child["run_id"])
        self.assertFalse(report["passed"])

    def test_E_cross_process_restores_version_experience_and_rejections(self):
        # 先留下一个已拒绝候选，再通过实际报告验证经验及发布Memory版本。
        rejected = self.candidate(suffix="bad")
        self.validate_pair(rejected, child_correct=False)
        scope = self.foundation.budget.create(1)
        source = self.foundation.run(self.base["version_id"], self.spec, scope["scope_id"], self.runner(correct=False))
        exp = self.foundation.experiences.create("检索缺证据时先核对关系", [EvidenceRef(source["run_id"], self.tasks[0].task_id)],
                                               {"task_family": "offline-fixture"})
        trial = self.candidate(experience_ids=[exp["experience_id"]], suffix="experience")
        report, _, _, _ = self.validate_pair(trial)
        self.foundation.experiences.verify(exp["experience_id"], report["evaluation_id"])
        self.foundation.accept(trial["candidate_id"])
        memory = self.candidate("memory", memory_ids=[exp["experience_id"]], suffix="validated")
        self.validate_pair(memory)
        accepted = self.foundation.accept(memory["candidate_id"])
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", str(ROOT/"scripts/run_harness.py"),
                                 "inspect", "--state-dir", self.temp.name], cwd=ROOT,
                                env=os.environ.copy(), capture_output=True, text=True, encoding="utf-8", check=True)
        restored = json.loads(result.stdout)
        self.assertEqual(restored["current_version"]["version_id"], accepted["version_id"])
        self.assertIn(exp["experience_id"], [e["experience_id"] for e in restored["active_experiences"]])
        self.assertIn(rejected["candidate_id"], [c["candidate_id"] for c in restored["candidates"] if c["status"] == "rejected"])
        self.store.rollback(self.base["version_id"], "恢复初始记忆快照")
        self.assertEqual(self.foundation.resume()["active_experiences"], [])
        self.assertEqual(self.store.read("experiences", exp["experience_id"])["status"], "verified")

    def test_E_single_observation_lifecycle_and_real_evidence_boundary(self):
        scope = self.foundation.budget.create(1)
        source = self.foundation.run(self.base["version_id"], self.spec, scope["scope_id"], self.runner(correct=False))
        ref = EvidenceRef(source["run_id"], self.tasks[0].task_id)
        exp = self.foundation.experiences.create("关系检索经验", [ref], {"scope": "fixture"})
        self.assertEqual(exp["status"], "pending")
        self.assertEqual(exp["evaluation_results"][0]["em"], 0)
        self.assertEqual(len(self.foundation.experiences.search("关系", statuses=("pending",))), 1)
        with self.assertRaises(ValueError):
            self.foundation.experiences.transition(exp["experience_id"], "verified", "直接升级")
        with self.assertRaises(ValueError):
            self.foundation.experiences.create("无效证据", [EvidenceRef("missing", "task")], {"scope": "fixture"})

    def test_E_conflicts_expiry_and_simulated_verification_are_explicit(self):
        scope = self.foundation.budget.create(1)
        source = self.foundation.run(self.base["version_id"], self.spec, scope["scope_id"], self.runner(correct=False))
        ref = EvidenceRef(source["run_id"], self.tasks[0].task_id)
        exp = self.foundation.experiences.create("证据保留经验", [ref], {"scope": "fixture"})
        trial = self.candidate(experience_ids=[exp["experience_id"]])
        report, _, _, _ = self.validate_pair(trial)
        verified = self.foundation.experiences.verify(exp["experience_id"], report["evaluation_id"])
        self.assertEqual(verified["verification_level"], "offline_simulated")
        self.assertEqual(self.foundation.experiences.search("证据"), [])
        self.assertEqual(len(self.foundation.experiences.search("证据", allow_simulated=True)), 1)
        conflict = self.foundation.experiences.transition(exp["experience_id"], "conflict", "存在相反结果", [ref])
        self.assertTrue(conflict["counter_evidence"])
        self.assertEqual(self.foundation.experiences.search("证据", allow_simulated=True), [])
        self.foundation.experiences.transition(exp["experience_id"], "expired", "适用版本已过期")
        with self.assertRaises(StateConflict):
            self.foundation.experiences.verify(exp["experience_id"], report["evaluation_id"], "旧验证不够")
        self.foundation.experiences.transition(exp["experience_id"], "retired", "已淘汰")
        self.foundation.experiences.transition(exp["experience_id"], "rejected", "反例保留")

    def test_F_artifact_corruption_blocks_accept_and_restore(self):
        candidate = self.candidate()
        self.validate_pair(candidate)
        ref = candidate["change"]["after"]
        path = self.store.root/ref["path"]
        original = path.read_bytes()
        path.write_bytes(b'{"corrupt":true}')
        with self.assertRaises(IntegrityError):
            self.foundation.accept(candidate["candidate_id"])
        self.assertEqual(self.store.current()["version_id"], self.base["version_id"])
        path.write_bytes(original)
        accepted = self.foundation.accept(candidate["candidate_id"])
        path.write_bytes(b'{"corrupt":true}')
        with self.assertRaises(IntegrityError):
            FoundationStore(self.temp.name).current()
        self.store.rollback(self.base["version_id"], "显式恢复未损坏祖先")
        self.assertEqual(self.store.current()["version_id"], self.base["version_id"])
        with self.assertRaises(IntegrityError):
            self.store.rollback(accepted["version_id"], "损坏目标不可恢复")

    def test_F_version_hash_mismatch_is_not_silently_repaired(self):
        with sqlite3.connect(self.store.db_path) as conn:
            row = conn.execute("SELECT payload FROM documents WHERE collection='versions' AND id=?", (self.base["version_id"],)).fetchone()
            changed = json.loads(row[0])
            changed["config_snapshot"]["model_config"]["temperature"] = 1
            # 更新外层存储摘要，保留错误的版本内容哈希，明确测试版本内部校验。
            conn.execute("UPDATE documents SET payload=?,digest=? WHERE collection='versions' AND id=?",
                         (json.dumps(changed), content_hash(changed), self.base["version_id"]))
        conn.close()
        with self.assertRaises(IntegrityError):
            FoundationStore(self.temp.name).current()

    def test_G_all_four_targets_use_same_public_gate_and_publish_path(self):
        for kind in TargetKind:
            with self.subTest(target=kind.value):
                old = self.store.current()
                candidate = self.candidate(kind.value, suffix=kind.value)
                draft = self.store.version(candidate["version_id"])
                for other in TargetKind:
                    if other != kind:
                        self.assertEqual(draft["artifacts"][other.value], old["artifacts"][other.value])
                self.assertTrue(candidate["change"]["diff"])
                self.validate_pair(candidate)
                accepted = self.foundation.accept(candidate["candidate_id"])
                self.assertEqual(accepted["artifacts"][kind.value], candidate["change"]["after"])

    def test_G_pending_memory_cannot_be_published(self):
        scope = self.foundation.budget.create(1)
        run = self.foundation.run(self.base["version_id"], self.spec, scope["scope_id"], self.runner(correct=False))
        exp = self.foundation.experiences.create("未验证经验", [EvidenceRef(run["run_id"], self.tasks[0].task_id)], {"scope": "fixture"})
        candidate = self.candidate("memory", memory_ids=[exp["experience_id"]])
        self.validate_pair(candidate)
        with self.assertRaises(StateConflict):
            self.foundation.accept(candidate["candidate_id"])
        self.assertEqual(self.store.current()["version_id"], self.base["version_id"])

    def test_budget_stops_safely_and_simulation_never_claims_api_spending(self):
        candidate = self.candidate()
        report, old, child, scope = self.validate_pair(candidate, cost=0.001, limit=0.005)
        self.assertEqual(old["status"], "completed")
        self.assertEqual(child["status"], "stopped")
        self.assertFalse(report["passed"])
        ledger = self.foundation.budget.read(scope["scope_id"])
        self.assertEqual(ledger["api_calls"], 0)
        self.assertEqual(ledger["actual_api_cost_cny"], 0)
        self.assertEqual(ledger["mode"], "simulation")
        self.assertLessEqual(ledger["accounted_cny"], ledger["limit_cny"])
        self.assertEqual(self.store.current()["version_id"], self.base["version_id"])

    def test_budget_scope_pending_and_post_validation_stop_veto_accept(self):
        candidate = self.candidate()
        report, _, _, scope = self.validate_pair(candidate)
        self.assertTrue(report["passed"])
        attempt = self.foundation.budget.reserve(scope["scope_id"], "unresolved-run", self.base["version_id"], None, 0.1)
        with self.assertRaises(StateConflict):
            self.foundation.accept(candidate["candidate_id"])
        self.foundation.budget.settle(attempt, 0, 0)
        with self.assertRaises(BudgetExceeded):
            self.foundation.budget.reserve(scope["scope_id"], "over-budget", self.base["version_id"], None, 10)
        with self.assertRaises(StateConflict):
            self.foundation.accept(candidate["candidate_id"])

    def test_budget_candidate_total_cap_is_separate_from_scope_limit(self):
        candidate = self.candidate(spec=replace(self.spec, max_total_cost_cny=0.001))
        report, _, _, scope = self.validate_pair(candidate, cost=0.001, limit=1)
        self.assertFalse(report["passed"])
        self.assertTrue(any("累计成本" in reason for reason in report["reasons"]))
        self.assertLess(self.foundation.budget.read(scope["scope_id"])["accounted_cny"], 1)

    def test_stale_parent_is_rejected_across_store_instances(self):
        first = self.candidate(suffix="first")
        second = self.candidate("tool", suffix="second")
        self.validate_pair(first)
        self.validate_pair(second)
        self.foundation.accept(first["candidate_id"])
        other = EvolutionFoundation(FoundationStore(self.temp.name))
        with self.assertRaises(StateConflict):
            other.accept(second["candidate_id"])
        self.assertEqual(other.resume()["current_version"]["version_id"], first["version_id"])

    def test_legacy_ledger_reader_is_explicit_read_only_and_scorer_unchanged(self):
        path = Path(self.temp.name)/"legacy.jsonl"
        rows = [dict(phase="old", model="fixture-model", cost_cny=0.01, seconds=1),
                dict(phase="new", parameters={"model": "fixture-model"}, cost_cny=0.02, seconds=2)]
        original = "\n".join(json.dumps(r) for r in rows)
        path.write_text(original, encoding="utf-8")
        normalized = read_legacy_usage(path)
        self.assertEqual([r["cost_kind"] for r in normalized], ["historical_estimate"]*2)
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertEqual(self.store.list("budgets"), [])
        self.assertEqual(score("No. 5", "No. 5"), (1, 1))
        self.assertEqual(score("world No. 5", "No. 5"), (0, 0.8))
        self.assertEqual(score("yes", "yes indeed"), (0, 0))

    def test_history_directories_are_not_allowed_as_state_root(self):
        with self.assertRaises(ValueError):
            FoundationStore(ROOT/"runs/round1")


if __name__ == "__main__":
    unittest.main()
