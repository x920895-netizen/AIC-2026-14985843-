"""在线 runner：本地 http.server 模拟 OpenAI 兼容端点。

覆盖：成功与鉴权、429 重试、500 重试、超时、usage 计费、usage 缺失、
JSON 模式、8 路并发、断点续跑。
"""

import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import EvolutionFoundation, FoundationStore, GateSpec, RuntimeTask
from harness.execution import OnlineExecutor
from harness.runners.mock import MockRunner
from harness.runners.openai_compatible import OpenAICompatibleRunner


def completion_body(text="ok", usage=None):
    body = {"choices": [{"message": {"role": "assistant", "content": text}}]}
    if usage is not None:
        body["usage"] = usage
    return body


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


class FakeOpenAI:
    """线程安全的假端点：按队列返回预设响应，并记录并发与请求体。"""

    def __init__(self):
        self.queue = []
        self.requests = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.default = (200, completion_body("default"), 0.0)
        self.server = QuietServer(("127.0.0.1", 0), self._handler())
        self.server.fake = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def push(self, status, body, delay=0.0):
        with self.lock:
            self.queue.append((status, body, delay))

    def next_response(self):
        with self.lock:
            return self.queue.pop(0) if self.queue else self.default

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) or b"{}"
                payload = json.loads(raw)
                status, body, delay = fake.next_response()
                with fake.lock:
                    fake.active += 1
                    fake.max_active = max(fake.max_active, fake.active)
                    fake.requests.append(dict(path=self.path, payload=payload,
                                              headers={k.lower(): v for k, v in self.headers.items()}))
                try:
                    if delay:
                        time.sleep(delay)
                    data = json.dumps(body).encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                finally:
                    with fake.lock:
                        fake.active -= 1

            def log_message(self, *args):
                pass
        return Handler

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class OnlineRunnerTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOpenAI()
        self.addCleanup(self.fake.close)

    def runner(self, **kwargs):
        params = dict(base_url=self.fake.base_url, model="test-model", api_key="test-key",
                      workers=4, max_retries=4, timeout=2.0, retry_backoff=0.0,
                      input_price_cny_per_million=1.0, output_price_cny_per_million=2.0)
        params.update(kwargs)
        return OpenAICompatibleRunner(**params)

    def test_success_records_usage_cost_and_authorization(self):
        self.fake.push(200, completion_body("Paris", {"prompt_tokens": 10, "completion_tokens": 5}))
        result = self.runner().complete([{"role": "user", "content": "capital?"}])
        self.assertIsNone(result.error)
        self.assertEqual(result.text, "Paris")
        self.assertEqual(result.attempts, 1)
        self.assertEqual((result.prompt_tokens, result.completion_tokens), (10, 5))
        self.assertAlmostEqual(result.cost_cny, (10*1.0+5*2.0)/1e6, places=12)
        self.assertTrue(result.usage_reported)
        request = self.fake.requests[0]
        self.assertEqual(request["headers"]["authorization"], "Bearer test-key")
        self.assertTrue(request["path"].endswith("/chat/completions"))

    def test_429_is_retried_then_succeeds(self):
        self.fake.push(429, {"error": "rate limit"})
        self.fake.push(200, completion_body("after-retry", {"prompt_tokens": 1, "completion_tokens": 1}))
        result = self.runner().complete([{"role": "user", "content": "q"}])
        self.assertIsNone(result.error, result.error)
        self.assertEqual(result.text, "after-retry")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(self.fake.requests), 2)

    def test_500_is_retried_then_succeeds(self):
        self.fake.push(500, {"error": "server"})
        self.fake.push(502, {"error": "bad gateway"})
        self.fake.push(200, completion_body("recovered", {"prompt_tokens": 2, "completion_tokens": 1}))
        result = self.runner().complete([{"role": "user", "content": "q"}])
        self.assertIsNone(result.error, result.error)
        self.assertEqual(result.attempts, 3)
        self.assertEqual(len(self.fake.requests), 3)

    def test_timeout_is_retried_and_reported(self):
        self.fake.push(200, completion_body("too-slow"), delay=0.5)
        self.fake.push(200, completion_body("too-slow"), delay=0.5)
        result = self.runner(timeout=0.15, max_retries=2).complete([{"role": "user", "content": "q"}])
        self.assertIsNotNone(result.error)
        self.assertIn("超时", result.error)
        self.assertEqual(result.attempts, 2)

    def test_missing_usage_uses_reservation_bound(self):
        self.fake.push(200, completion_body("no-usage"))
        runner = self.runner()
        messages = [{"role": "user", "content": "q"}]
        result = runner.complete(messages)
        self.assertIsNone(result.error)
        self.assertFalse(result.usage_reported)
        bound = runner.estimate_messages_cost(messages)
        self.assertGreater(bound, 0)
        self.assertAlmostEqual(result.cost_cny, bound, places=12)

    def test_non_retryable_400_fails_fast(self):
        self.fake.push(400, {"error": "bad request"})
        result = self.runner().complete([{"role": "user", "content": "q"}])
        self.assertIsNotNone(result.error)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(self.fake.requests), 1)

    def test_json_mode_sets_response_format(self):
        self.fake.push(200, completion_body('{"ok": true}'))
        runner = self.runner(response_format={"type": "json_object"})
        result = runner.complete([{"role": "user", "content": "q"}])
        self.assertIsNone(result.error)
        self.assertEqual(self.fake.requests[0]["payload"]["response_format"], {"type": "json_object"})

    def test_missing_api_key_fails_before_any_request(self):
        result = self.runner(api_key=None, api_key_env="MISSING_KEY_FOR_TEST").complete(
            [{"role": "user", "content": "q"}])
        self.assertIsNotNone(result.error)
        self.assertIn("API Key", result.error)
        self.assertEqual(self.fake.requests, [])


class OnlineExecutorTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOpenAI()
        self.addCleanup(self.fake.close)
        self.temp = tempfile.TemporaryDirectory(prefix="online-exec-")
        self.addCleanup(self.temp.cleanup)
        self.store = FoundationStore(self.temp.name)
        self.engine = EvolutionFoundation(self.store)
        artifacts = {
            "prompt": {"components": {"final_answer.predict": "请回答"}},
            "memory": {"experience_ids": [], "retrieval_policy": {}, "lifecycle_policy": {}},
            "tool": {"tools": [], "selection_policy": {}, "risk_constraints": {}},
            "skill": {"definitions": [], "selection_policy": {}, "lifecycle_policy": {}},
        }
        refs = {k: self.store.write_artifact(k, v) for k, v in artifacts.items()}
        self.config = {"model": "test-model", "temperature": 0}
        self.store.initialize(refs, {"model_config": self.config})

    def dataset(self, n=4):
        tasks = [RuntimeTask(f"q{i}", f"问题{i}") for i in range(1, n+1)]
        labels = {t.task_id: f"answer {i}" for i, t in enumerate(tasks, 1)}
        dataset_id = f"ds-{n}"
        self.engine.register_dataset(dataset_id, tasks, labels)
        spec = GateSpec(dataset_id, tuple(t.task_id for t in tasks), self.config,
                        require_offline=False, max_total_cost_cny=10.0)
        return tasks, labels, spec

    def runner(self, **kwargs):
        params = dict(base_url=self.fake.base_url, model="test-model", api_key="test-key",
                      workers=8, max_retries=2, timeout=5.0, retry_backoff=0.0,
                      input_price_cny_per_million=1.0, output_price_cny_per_million=1.0)
        params.update(kwargs)
        return OpenAICompatibleRunner(**params)

    def test_executor_records_real_cost_and_calls(self):
        tasks, labels, spec = self.dataset(2)
        for i in range(1, 3):
            self.fake.push(200, completion_body(labels[f"q{i}"], {"prompt_tokens": 100, "completion_tokens": 50}))
        scope = self.engine.budget.create(10.0)
        run = self.engine.run(self.store.current()["version_id"], spec, scope["scope_id"],
                              self.runner(), executor=OnlineExecutor(self.store, workers=2))
        self.assertEqual(run["status"], "completed", run.get("termination_reason"))
        self.assertEqual(run["mode"], "real")
        self.assertEqual(run["api_calls"], 2)
        expected = 2*((100*1.0+50*1.0)/1e6)
        self.assertAlmostEqual(run["actual_api_cost_cny"], expected, places=12)
        budget = self.engine.budget.read(scope["scope_id"])
        self.assertAlmostEqual(budget["actual_api_cost_cny"], expected, places=12)
        self.assertEqual(budget["api_calls"], 2)
        self.assertEqual(budget["simulated_calls"], 0)

    def test_concurrency_uses_eight_workers(self):
        tasks, labels, spec = self.dataset(8)
        for _ in range(8):
            self.fake.push(200, completion_body("x", {"prompt_tokens": 1, "completion_tokens": 1}), delay=0.2)
        scope = self.engine.budget.create(10.0)
        run = self.engine.run(self.store.current()["version_id"], spec, scope["scope_id"],
                              self.runner(workers=8), executor=OnlineExecutor(self.store, workers=8))
        self.assertEqual(run["status"], "completed")
        self.assertEqual(len(self.fake.requests), 8)
        self.assertGreaterEqual(self.fake.max_active, 4, "并发执行未生效")
        self.assertEqual(run["workers"], 8)

    def test_resume_skips_completed_tasks(self):
        tasks, labels, spec = self.dataset(4)
        calls = []

        def responder(task, prompt, artifacts):
            calls.append(task.task_id)
            if task.task_id == "q3" and calls.count("q3") == 1:
                raise RuntimeError("模拟一次执行失败")
            return labels[task.task_id]

        scope = self.engine.budget.create(10.0)
        executor = OnlineExecutor(self.store, workers=1)
        runner = MockRunner(responder, workers=1, unit_cost_cny=0.001)
        first = self.engine.run(self.store.current()["version_id"], spec, scope["scope_id"],
                                runner, executor=executor)
        self.assertEqual(first["status"], "failed")
        completed_before = {r["task_id"] for r in first["outputs"] if r["status"] == "completed"}
        self.assertNotIn("q3", completed_before, "失败任务不应记为完成")
        self.assertIn("q1", completed_before)

        def resumed_responder(task, prompt, artifacts):
            calls.append(task.task_id)
            return labels[task.task_id]

        resumed = self.engine.run(self.store.current()["version_id"], spec, scope["scope_id"],
                                  MockRunner(resumed_responder, workers=1, unit_cost_cny=0.001),
                                  executor=executor, resume_run_id=first["run_id"])
        self.assertEqual(resumed["status"], "completed", resumed.get("termination_reason"))
        keys = [(r["task_id"], r["repeat"]) for r in resumed["outputs"]]
        self.assertEqual(len(keys), 4)
        self.assertEqual(len(set(keys)), 4, "续跑后出现重复任务")
        self.assertEqual(resumed["run_id"], first["run_id"])
        self.assertEqual(calls.count("q1"), 1)
        self.assertEqual(calls.count("q2"), 1)
        self.assertEqual(calls.count("q3"), 2, "失败任务应且只应重跑一次")
        self.assertEqual(calls.count("q4"), 1)


if __name__ == "__main__":
    unittest.main()
