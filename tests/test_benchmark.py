"""检查四模块轨迹、官方反馈接口及费用限额，不调用真实模型。"""

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import dspy
import gepa
import httpx
from openai import APIConnectionError, RateLimitError
from gepa.core.adapter import EvaluationBatch
from dspy.utils import DummyLM
from hotpot_adapter import HotpotAdapter, f1_score
from model_client import BudgetExceeded, GenerationTruncated, ModelClient, load_usage_records


class BenchmarkTests(unittest.TestCase):
    def test_four_modules_and_official_feedback(self):
        docs = ["Paris | Paris is the capital of France.", "France | France is a country in Europe."]
        retriever = SimpleNamespace(search=lambda query, k: docs)
        client = SimpleNamespace(local=threading.local(), phase="test", spent=0)
        lm = DummyLM([
            {"reasoning": "r", "summary": "Paris is in France."},
            {"reasoning": "r", "query": "France country"},
            {"reasoning": "r", "summary": "France is the country containing Paris."},
            {"reasoning": "r", "answer": "France"},
        ])
        row = {"id": "fixture", "split": "train", "question": "Which country contains Paris?", "answer": "France",
               "supporting_facts": {"title": ["Paris", "France"], "sent_id": [0, 0]},
               "context": {"title": ["Paris", "France"], "sentences": [["Paris is in France."], ["France is a country."]]}}
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as temp:
            adapter = HotpotAdapter(ROOT, retriever, lm, client, temp, workers=1)
            candidate = adapter.seed_candidate()
            batch = adapter.evaluate([row], candidate, capture_traces=True)
            self.assertEqual(batch.scores, [1.0])
            self.assertEqual(len(batch.outputs[0]["steps"]), 4)
            feedback = adapter.make_reflective_dataset(candidate, batch, list(candidate))
            self.assertEqual(set(feedback), set(candidate))
            for records in feedback.values():
                self.assertTrue(records[0]["Feedback"])

    def test_budget_blocks_before_sending(self):
        config = json.loads((ROOT / "benchmark_config.json").read_text(encoding="utf-8"))
        config["budget_cny"] = 0
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as temp, patch.dict("os.environ", {"DASHSCOPE_API_KEY": "test-only"}):
            client = ModelClient(config, temp)
            with patch.object(client.api.chat.completions, "create") as create:
                with self.assertRaises(BudgetExceeded):
                    client.call("task", [{"role": "user", "content": "hello"}])
                create.assert_not_called()

    def test_hotpot_f1_yes_no_and_partial_answers(self):
        self.assertEqual(f1_score("yes", "yes indeed"), 0)
        self.assertEqual(f1_score("The Eiffel Tower", "Eiffel Tower"), 1)
        self.assertAlmostEqual(f1_score("Paris France", "Paris"), 2 / 3)

    def test_truncation_is_a_scored_failure_with_feedback(self):
        retriever = SimpleNamespace(search=lambda query, k: ["Paris | Paris is in France."])
        client = SimpleNamespace(local=threading.local(), phase="test", spent=0)
        lm = DummyLM([])
        row = {"id": "truncated", "split": "train", "question": "Where is Paris?", "answer": "France",
               "supporting_facts": {"title": ["Paris"], "sent_id": [0]},
               "context": {"title": ["Paris"], "sentences": [["Paris is in France."]]}}
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as temp, patch.object(lm, "forward", side_effect=GenerationTruncated("limit")):
            adapter = HotpotAdapter(ROOT, retriever, lm, client, temp, workers=1)
            candidate = adapter.seed_candidate()
            batch = adapter.evaluate([row], candidate, capture_traces=True)
            self.assertEqual(batch.scores, [0.0])
            self.assertEqual(batch.outputs[0]["hop1_support_recall"], 1)
            feedback = adapter.make_reflective_dataset(candidate, batch, list(candidate))
            self.assertTrue(all("failed" in records[0]["Feedback"] for records in feedback.values()))

    def test_calibration_usage_is_included_in_total_budget(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as temp:
            calibration = Path(temp) / "round1-calibration-2048"
            calibration.mkdir()
            (calibration / "api_calls.jsonl").write_text(json.dumps({"phase": "smoke", "cost_cny": 0.2}) + "\n", encoding="utf-8")
            records = load_usage_records(Path(temp) / "round1")
            self.assertEqual(sum(r["cost_cny"] for r in records), 0.2)
            self.assertEqual(records[0]["phase"], "calibration_2048_smoke")

    def test_gepa_uses_official_reflector_and_accepts_candidate(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as temp:
            adapter = HotpotAdapter(ROOT, None, None, None, temp, workers=1)
            seed = adapter.seed_candidate()
            def evaluate(batch, candidate, capture_traces=False):
                score = float(any("improved" in text for text in candidate.values()))
                return EvaluationBatch(outputs=[{} for _ in batch], scores=[score for _ in batch],
                                       trajectories=[{} for _ in batch] if capture_traces else None)
            with patch.object(adapter, "evaluate", side_effect=evaluate), patch.object(adapter, "make_reflective_dataset", side_effect=lambda candidate, batch, names: {name: [{"Inputs": "x", "Generated Outputs": "y", "Feedback": "Improve correctness"}] for name in names}):
                reflector = unittest.mock.Mock(return_value="```\nimproved instructions\n```")
                result = gepa.optimize(seed_candidate=seed, trainset=[{}], valset=[{}], adapter=adapter,
                    reflection_lm=reflector, max_metric_calls=4, reflection_minibatch_size=1,
                    run_dir=temp, seed=1)
                self.assertGreaterEqual(reflector.call_count, 1)
                self.assertGreaterEqual(result.num_candidates, 2)
                self.assertEqual(result.best_score, 1)

    def test_rate_limit_waits_and_retries_only_a_bounded_number(self):
        config = json.loads((ROOT / "benchmark_config.json").read_text(encoding="utf-8"))
        response = httpx.Response(429, headers={"retry-after": "2"}, request=httpx.Request("POST", "https://example.invalid"))
        failure = RateLimitError("limited", response=response, body={})
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as temp, patch.dict("os.environ", {"DASHSCOPE_API_KEY": "test-only"}):
            client = ModelClient(config, temp)
            with patch.object(client, "_call_once", side_effect=[failure, "ok"]) as call, patch("model_client.time.sleep") as sleep:
                self.assertEqual(client.call("task", []), "ok")
                self.assertEqual(call.call_count, 2)
                sleep.assert_called_once_with(2)
            with patch.object(client, "_call_once", side_effect=failure) as call, patch("model_client.time.sleep"):
                with self.assertRaises(RateLimitError):
                    client.call("task", [])
                self.assertEqual(call.call_count, 4)
            disconnected = APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))
            with patch.object(client, "_call_once", side_effect=[disconnected, "reconnected"]) as call, patch("model_client.time.sleep") as sleep:
                self.assertEqual(client.call("task", []), "reconnected")
                self.assertEqual(call.call_count, 2)
                sleep.assert_called_once_with(15)


if __name__ == "__main__":
    unittest.main()
