"""决策凭证：字段稳定性、链完整性、篡改检测与事务一致性。"""

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("DSPY_CACHEDIR", str(ROOT / ".cache/dspy"))

from harness import CandidateProposal, EvolutionFoundation, FoundationStore, GateSpec, RuntimeTask, TargetKind
from harness.contracts import canonical, content_hash
from harness.receipt import (GENESIS, RECEIPT_FIELDS, ReceiptLedger, make_receipt,
                             receipt_hash, verify_chain)
from harness.storage import IntegrityError, StateConflict

HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="receipt-")
        self.addCleanup(self.temp.cleanup)
        self.store = FoundationStore(self.temp.name)
        self.engine = EvolutionFoundation(self.store)
        artifacts = {
            "prompt": {"components": {"final_answer.predict": "凭证测试初始指令"}},
            "memory": {"experience_ids": [], "retrieval_policy": {}, "lifecycle_policy": {}},
            "tool": {"tools": [], "selection_policy": {}, "risk_constraints": {}},
            "skill": {"definitions": [], "selection_policy": {}, "lifecycle_policy": {}},
        }
        refs = {k: self.store.write_artifact(k, v) for k, v in artifacts.items()}
        self.config = {"model_config": {"model": "offline-deterministic", "temperature": 0}}
        self.base = self.store.initialize(refs, self.config)
        self.tasks = [RuntimeTask(f"rcpt-{i}", f"凭证问题{i}") for i in range(4)]
        self.labels = {t.task_id: f"answer {i}" for i, t in enumerate(self.tasks)}
        self.engine.register_dataset("receipt-set", self.tasks, self.labels)
        self.spec = GateSpec("receipt-set", tuple(t.task_id for t in self.tasks),
                             self.config["model_config"])

    def make_candidate(self, suffix, spec=None):
        parent = self.store.current()
        ref = self.store.write_artifact("prompt", {"components": {"final_answer.predict": suffix}})
        return self.engine.register(CandidateProposal(parent["version_id"], TargetKind.PROMPT, ref,
                                                      spec or self.spec, "凭证链测试候选"))

    def run_pair(self, candidate, child_correct=True):
        scope = self.engine.budget.create(1.0)
        old = self.engine.run(candidate["parent_version_id"], self.spec, scope["scope_id"],
                              lambda task, *_: "incorrect", projected_cost_per_task=0.001)
        new = self.engine.run(candidate["version_id"], self.spec, scope["scope_id"],
                              lambda task, *_: (self.labels[task.task_id] if child_correct else "incorrect"),
                              candidate_id=candidate["candidate_id"], projected_cost_per_task=0.001)
        return self.engine.validate(candidate["candidate_id"], old["run_id"], new["run_id"])

    def accept_one(self):
        candidate = self.make_candidate("可接受候选")
        report = self.run_pair(candidate)
        self.assertTrue(report["passed"], report["reasons"])
        return candidate, report

    def reject_one(self):
        candidate = self.make_candidate("退化候选")
        report = self.run_pair(candidate, child_correct=False)
        self.assertFalse(report["passed"])
        return candidate, report

    def receipts(self):
        with self.store.connection() as conn:
            return self.engine.receipts.all(conn)

    def test_accept_and_reject_form_one_chain(self):
        accepted, _ = self.accept_one()
        self.engine.accept(accepted["candidate_id"])
        rejected, _ = self.reject_one()
        rows = self.receipts()
        self.assertEqual([r["decision"] for r in rows], ["accept", "reject"])
        self.assertEqual(rows[0]["prev_receipt_hash"], GENESIS)
        self.assertEqual(rows[1]["prev_receipt_hash"], rows[0]["receipt_hash"])
        self.assertEqual(rows[1]["candidate_id"], rejected["candidate_id"])
        self.assertEqual(rows[0]["parent_version"], self.base["version_id"])
        with self.store.connection() as conn:
            result = verify_chain(conn)
        self.assertEqual(result, dict(verified=True, count=2, head=rows[-1]["receipt_hash"]))

    def test_receipt_fields_are_fixed_and_hashes_are_sha256(self):
        accepted, _ = self.accept_one()
        self.engine.accept(accepted["candidate_id"])
        row = self.receipts()[0]
        self.assertEqual(set(row), set(RECEIPT_FIELDS))
        for name in ("receipt_hash", "gate_spec_hash", "diff_hash", "metrics_hash", "evidence_hash"):
            with self.subTest(field=name):
                self.assertRegex(row[name], HEX64)
        self.assertEqual(row["receipt_hash"], receipt_hash(row))
        without_hash = {k: v for k, v in row.items() if k != "receipt_hash"}
        self.assertEqual(row["receipt_hash"], content_hash(without_hash))
        self.assertEqual(row["profile"], "release")
        self.assertEqual(row["gate_spec_hash"], content_hash(self.spec.to_dict()))

    def test_receipt_hash_is_deterministic_for_same_fields(self):
        kwargs = dict(decision="reject", candidate_id="candidate-x", parent_version="version-a",
                      candidate_version="version-b", profile="screen", gate_spec_hash="a" * 64,
                      diff_hash="b" * 64, metrics_hash="c" * 64, evidence_hash="d" * 64,
                      prev_receipt_hash="e" * 64, created_at="2026-10-29T00:00:00+00:00",
                      receipt_id="receipt-fixed")
        first = make_receipt(**kwargs)
        second = make_receipt(**kwargs)
        self.assertEqual(first, second)
        forged = dict(first, receipt_hash="f" * 64)
        self.assertEqual(receipt_hash(forged), first["receipt_hash"])

    def test_content_tampering_is_detected(self):
        accepted, _ = self.accept_one()
        self.engine.accept(accepted["candidate_id"])
        with self.store.connection() as conn:
            row = conn.execute("SELECT seq,payload FROM receipts").fetchone()
            payload = json.loads(row["payload"])
            payload["metrics_hash"] = "0" * 64
            conn.execute("UPDATE receipts SET payload=? WHERE seq=?",
                         (canonical(payload).decode("utf-8"), row["seq"]))
            with self.assertRaises(IntegrityError):
                verify_chain(conn)

    def test_forged_self_consistent_receipt_breaks_chain(self):
        accepted, _ = self.accept_one()
        self.engine.accept(accepted["candidate_id"])
        self.reject_one()
        with self.store.connection() as conn:
            row = conn.execute("SELECT seq,payload FROM receipts ORDER BY seq LIMIT 1").fetchone()
            payload = json.loads(row["payload"])
            payload["prev_receipt_hash"] = "0" * 64
            payload["receipt_hash"] = receipt_hash(payload)
            conn.execute("UPDATE receipts SET payload=?, digest=? WHERE seq=?",
                         (canonical(payload).decode("utf-8"), payload["receipt_hash"], row["seq"]))
            with self.assertRaises(IntegrityError) as caught:
                verify_chain(conn)
        self.assertIn("凭证链断裂", str(caught.exception))

    def test_duplicate_decision_receipt_is_refused(self):
        accepted, report = self.accept_one()
        self.engine.accept(accepted["candidate_id"])
        with self.store.transaction("duplicate-test") as conn:
            candidate = self.store.get(conn, "candidates", accepted["candidate_id"])
            self.assertEqual(len(self.engine.receipts.all(conn)), 1)
            with self.assertRaises(StateConflict):
                self.engine.receipts.record(conn, "accept", candidate, report)

    def test_receipt_is_written_in_the_same_transaction_as_the_decision(self):
        candidate = self.make_candidate("事务回滚候选")

        def fault(name):
            if name == "validation.before_commit":
                raise OSError("注入凭证写入前故障")

        self.store.fault_hook = fault
        with self.assertRaises(OSError):
            self.run_pair(candidate, child_correct=False)
        self.store.fault_hook = None
        self.assertEqual(self.receipts(), [])
        self.assertEqual(self.store.read("candidates", candidate["candidate_id"])["status"], "registered")
        self.assertEqual(self.store.current()["version_id"], self.base["version_id"])

    def test_verified_chain_survives_reopen(self):
        accepted, _ = self.accept_one()
        self.engine.accept(accepted["candidate_id"])
        reopened = EvolutionFoundation(FoundationStore(self.temp.name))
        state = reopened.resume()
        self.assertEqual([r["decision"] for r in state["receipts"]], ["accept"])
        with reopened.store.connection() as conn:
            self.assertTrue(verify_chain(conn)["verified"])


if __name__ == "__main__":
    unittest.main()
