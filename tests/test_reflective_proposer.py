"""反思提议器：JSON 解析、重试、内容校验与失败关闭。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import FoundationStore, GateSpec
from harness.contracts import TargetKind
from harness.proposers.base import FailureCase, ProposalContext
from harness.proposers.reflective import ReflectiveProposer
from harness.runners.base import RunnerResult

CURRENT = "请回答校园制度问题。"


def payload(**overrides):
    base = {"diagnosis": "模型缺少对限定词的核对", "change_summary": "补充先定位实体再核对名称",
            "expected_gain": "预期减少名称截断错误", "risk": "指令变长可能增加成本",
            "new_prompt": "先定位每一跳的实体，再核对最终答案的完整名称。"}
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, messages, response_format=None):
        self.calls.append(dict(messages=messages, response_format=response_format))
        if not self.replies:
            return RunnerResult(text="", external_api=True)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, RunnerResult):
            return reply
        return RunnerResult(text=reply, external_api=True)


class ReflectiveProposerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="proposer-")
        self.addCleanup(self.temp.cleanup)
        self.store = FoundationStore(self.temp.name)
        artifacts = {
            "prompt": {"components": {"final_answer.predict": CURRENT}},
            "memory": {"experience_ids": [], "retrieval_policy": {}, "lifecycle_policy": {}},
            "tool": {"tools": [], "selection_policy": {}, "risk_constraints": {}},
            "skill": {"definitions": [], "selection_policy": {}, "lifecycle_policy": {}},
        }
        self.refs = {k: self.store.write_artifact(k, v) for k, v in artifacts.items()}
        self.version = self.store.initialize(self.refs, {"model_config": {"model": "test", "temperature": 0}})
        self.gate = GateSpec("ds", ("q1", "q2"), {"model": "test", "temperature": 0})

    def context(self, failures=2):
        artifacts = {k: self.store.load_artifact(ref) for k, ref in self.version["artifacts"].items()}
        cases = tuple(FailureCase(task_id=f"q{i}", question=f"问题{i}", prediction="错误答案",
                                  gold=f"答案{i}", em=0.0, f1=0.5) for i in range(1, failures+1))
        return ProposalContext(parent_version=self.version, artifacts=artifacts, gate=self.gate,
                               failures=cases, gate_reasons=("主指标EM增益不足",), round_index=2)

    def proposer(self, replies, **kwargs):
        client = FakeClient(replies)
        return ReflectiveProposer(self.store, client, **kwargs), client

    def test_valid_json_creates_candidate_proposal(self):
        proposer, client = self.proposer([payload()])
        proposal = proposer.propose(self.context())
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.parent_version_id, self.version["version_id"])
        self.assertEqual(proposal.target, TargetKind.PROMPT)
        self.assertEqual(proposal.gate, self.gate)
        self.assertIn("缺少对限定词", proposal.basis)
        self.assertIn("减少名称截断", proposal.expected_benefit)
        artifact = self.store.load_artifact(proposal.artifact_ref)
        self.assertEqual(artifact["components"]["final_answer.predict"],
                         "先定位每一跳的实体，再核对最终答案的完整名称。")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["response_format"], {"type": "json_object"})

    def test_markdown_fenced_json_is_parsed(self):
        proposer, _ = self.proposer(["```json\n"+payload()+"\n```"])
        self.assertIsNotNone(proposer.propose(self.context()))

    def test_invalid_json_retries_once_then_succeeds(self):
        proposer, client = self.proposer(["这不是 JSON", payload()])
        proposal = proposer.propose(self.context())
        self.assertIsNotNone(proposal)
        self.assertEqual(len(client.calls), 2)
        retry_text = client.calls[-1]["messages"][-1]["content"]
        self.assertIn("上一次输出被拒绝", retry_text)

    def test_invalid_json_twice_returns_none(self):
        proposer, client = self.proposer(["nope", "{broken"])
        self.assertIsNone(proposer.propose(self.context()))
        self.assertEqual(len(client.calls), 2)

    def test_empty_prompt_is_rejected_after_retry(self):
        proposer, client = self.proposer([payload(new_prompt="   "), payload(new_prompt="")])
        self.assertIsNone(proposer.propose(self.context()))
        self.assertEqual(len(client.calls), 2)
        self.assertIn("不能为空", client.calls[-1]["messages"][-1]["content"])

    def test_unchanged_prompt_is_rejected(self):
        proposer, client = self.proposer([payload(new_prompt=CURRENT), payload(new_prompt=CURRENT)])
        self.assertIsNone(proposer.propose(self.context()))
        self.assertIn("与当前指令相同", client.calls[-1]["messages"][-1]["content"])

    def test_overlong_prompt_is_rejected(self):
        proposer, _ = self.proposer([payload(new_prompt="x"*200), payload(new_prompt="y"*200)],
                                    max_prompt_chars=100)
        self.assertIsNone(proposer.propose(self.context()))

    def test_missing_field_is_rejected(self):
        bad = json.dumps({"diagnosis": "d", "change_summary": "c", "expected_gain": "e",
                          "risk": "r"}, ensure_ascii=False)
        proposer, client = self.proposer([bad, bad])
        self.assertIsNone(proposer.propose(self.context()))
        self.assertEqual(len(client.calls), 2)

    def test_client_error_returns_none(self):
        proposer, _ = self.proposer([RunnerResult(external_api=True, error="HTTP 500")])
        self.assertIsNone(proposer.propose(self.context()))

    def test_client_exception_returns_none(self):
        proposer, _ = self.proposer([RuntimeError("boom")])
        self.assertIsNone(proposer.propose(self.context()))

    def test_prompt_contains_failures_gate_reasons_and_cap(self):
        proposer, client = self.proposer([payload()])
        proposer.propose(self.context(failures=10))
        text = client.calls[0]["messages"][-1]["content"]
        self.assertIn("主指标EM增益不足", text)
        self.assertIn("问题1", text)
        self.assertIn("错误答案", text)
        self.assertIn("答案1", text)
        self.assertIn("最近失败样本（8 条", text)
        self.assertNotIn("问题10", text)


if __name__ == "__main__":
    unittest.main()
