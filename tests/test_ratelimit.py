"""并发与限流：令牌桶、Retry-After 解析，以及 runner 尊重服务端等待时间。"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.execution import OnlineExecutor
from harness.ratelimit import RateLimitTimeout, TokenBucket, parse_retry_after
from harness.runners.openai_compatible import OpenAICompatibleRunner
from harness.storage import FoundationStore


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def completion_body(text="ok"):
    return json.dumps({"choices": [{"message": {"role": "assistant", "content": text}}],
                       "usage": {"prompt_tokens": 1, "completion_tokens": 1}})


class TokenBucketTests(unittest.TestCase):
    def bucket(self, rate=10.0, burst=5.0):
        fake = FakeClock()
        return TokenBucket(rate, burst, clock=fake.clock, sleep=fake.sleep), fake

    def test_bucket_starts_full_and_blocks_when_empty(self):
        bucket, _ = self.bucket()
        self.assertEqual([bucket.try_acquire() for _ in range(5)], [True]*5)
        self.assertFalse(bucket.try_acquire())
        self.assertEqual(bucket.stats()["acquired"], 5)

    def test_refills_at_configured_rate(self):
        bucket, fake = self.bucket(rate=10.0, burst=5.0)
        for _ in range(5):
            bucket.try_acquire()
        fake.now += 0.2
        self.assertTrue(bucket.try_acquire(), "0.2s 应按 10/s 补回 2 个令牌")
        self.assertTrue(bucket.try_acquire())
        self.assertFalse(bucket.try_acquire())

    def test_acquire_blocks_until_token_available(self):
        bucket, fake = self.bucket(rate=4.0, burst=1.0)
        self.assertTrue(bucket.try_acquire())
        waited = bucket.acquire()
        self.assertAlmostEqual(waited, 0.25, places=6)
        self.assertAlmostEqual(fake.now, 0.25, places=6)
        self.assertEqual(len(fake.slept), 1)

    def test_acquire_times_out(self):
        bucket, fake = self.bucket(rate=1.0, burst=1.0)
        bucket.try_acquire()
        with self.assertRaises(RateLimitTimeout):
            bucket.acquire(timeout=0.5)
        self.assertEqual(fake.slept, [], "超时前不应真的睡过")

    def test_stats_reports_wait_and_throughput(self):
        bucket, fake = self.bucket(rate=2.0, burst=1.0)
        bucket.acquire()
        bucket.acquire()
        stats = bucket.stats()
        self.assertEqual(stats["rate"], 2.0)
        self.assertEqual(stats["burst"], 1.0)
        self.assertEqual(stats["acquired"], 2)
        self.assertAlmostEqual(stats["total_wait_s"], 0.5, places=6)

    def test_rejects_invalid_configuration(self):
        with self.assertRaises(ValueError):
            TokenBucket(0)
        with self.assertRaises(ValueError):
            TokenBucket(-1)
        with self.assertRaises(ValueError):
            TokenBucket(1, 0)
        bucket, _ = self.bucket(rate=10.0, burst=2.0)
        with self.assertRaises(ValueError):
            bucket.acquire(3.0)
        with self.assertRaises(ValueError):
            bucket.acquire(0)


class RetryAfterParsingTests(unittest.TestCase):
    def test_numeric_seconds(self):
        self.assertEqual(parse_retry_after("3"), 3.0)
        self.assertEqual(parse_retry_after(" 2.5 "), 2.5)
        self.assertEqual(parse_retry_after(0), 0.0)

    def test_invalid_values_return_none(self):
        for value in (None, "", "   ", "abc", "-1", "-0.5", "NaN", object()):
            self.assertIsNone(parse_retry_after(value), value)

    def test_http_date_is_converted_to_seconds(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        self.assertAlmostEqual(
            parse_retry_after("Thu, 01 Jan 2026 12:00:10 GMT", now=now), 10.0, places=6)
        past = now - timedelta(seconds=30)
        header = past.strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.assertIsNone(parse_retry_after(header, now=now))

    def test_cap_is_applied(self):
        self.assertEqual(parse_retry_after("120"), 60.0)
        self.assertEqual(parse_retry_after("120", cap=5), 5.0)
        self.assertEqual(parse_retry_after("999999"), 60.0)


class RunnerRetryAfterTests(unittest.TestCase):
    def runner(self, responses, **kwargs):
        fake = FakeClock()
        runner = OpenAICompatibleRunner(base_url="http://127.0.0.1:1/v1", model="m", api_key="k",
                                        max_retries=4, retry_backoff=0.0, sleep=fake.sleep,
                                        timeout=1.0, **kwargs)
        queue = list(responses)
        calls = []

        def fake_post(data, headers):
            calls.append(dict(headers))
            return queue.pop(0)

        runner._post = fake_post
        runner.calls = calls
        return runner, fake

    def test_429_with_retry_after_is_honored(self):
        runner, fake = self.runner([(429, '{"error":"limit"}', {"Retry-After": "7"}),
                                    (200, completion_body("ok"), {})])
        result = runner.complete([{"role": "user", "content": "q"}])
        self.assertIsNone(result.error, result.error)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(fake.slept, [7.0], "必须按服务端给的 Retry-After 等待")
        self.assertEqual(runner.retry_after_honored, 1)
        self.assertAlmostEqual(runner.last_retry_after, 7.0)

    def test_retry_after_header_lookup_is_case_insensitive(self):
        runner, fake = self.runner([(503, "{}", {"retry-after": "2"}),
                                    (200, completion_body("ok"), {})])
        result = runner.complete([{"role": "user", "content": "q"}])
        self.assertIsNone(result.error, result.error)
        self.assertEqual(fake.slept, [2.0])

    def test_retry_after_is_capped(self):
        runner, fake = self.runner([(429, "{}", {"Retry-After": "999"}),
                                    (200, completion_body("ok"), {})], max_retry_after=30.0)
        runner.complete([{"role": "user", "content": "q"}])
        self.assertEqual(fake.slept, [30.0])

    def test_500_with_retry_after_falls_back_to_backoff(self):
        runner, fake = self.runner([(500, "{}", {"Retry-After": "9"}),
                                    (200, completion_body("ok"), {})])
        runner.complete([{"role": "user", "content": "q"}])
        self.assertEqual(fake.slept, [0.0], "500 不声明支持 Retry-After，应走指数退避")
        self.assertEqual(runner.retry_after_honored, 0)

    def test_headers_survive_retries(self):
        # 回归测试：重试时不得把响应头当成请求头（会丢 Authorization）。
        runner, _ = self.runner([(500, "{}", {"Content-Length": "999", "Retry-After": "1"}),
                                 (200, completion_body("ok"), {})])
        result = runner.complete([{"role": "user", "content": "q"}])
        self.assertIsNone(result.error, result.error)
        self.assertEqual(len(runner.calls), 2)
        for headers in runner.calls:
            self.assertEqual(headers.get("Authorization"), "Bearer k")
            self.assertEqual(headers.get("Content-Type"), "application/json")
            self.assertNotIn("Content-Length", headers)

    def test_non_retryable_status_fails_fast_without_sleeping(self):
        runner, fake = self.runner([(400, '{"error":"bad"}', {})])
        result = runner.complete([{"role": "user", "content": "q"}])
        self.assertIsNotNone(result.error)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(fake.slept, [])
        self.assertEqual(len(runner.calls), 1)

    def test_runner_level_rate_limiter_is_used_per_attempt(self):
        bucket = TokenBucket(1000.0, 100.0)
        runner, _ = self.runner([(500, "{}", {}), (200, completion_body("ok"), {})],
                                rate_limiter=bucket)
        runner.complete([{"role": "user", "content": "q"}])
        self.assertEqual(bucket.stats()["acquired"], 2, "每次尝试都应先取令牌")


class ExecutorRateLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ratelimit-")
        self.addCleanup(self.temp.cleanup)
        self.store = FoundationStore(Path(self.temp.name)/"state")

    def test_numeric_rate_limit_is_wrapped_in_token_bucket(self):
        executor = OnlineExecutor(self.store, rate_limit=5)
        self.assertIsInstance(executor.rate_limiter, TokenBucket)
        self.assertEqual(executor.rate_limiter.rate, 5.0)

    def test_token_bucket_is_accepted_as_is(self):
        bucket = TokenBucket(3, 3)
        executor = OnlineExecutor(self.store, rate_limit=bucket)
        self.assertIs(executor.rate_limiter, bucket)

    def test_invalid_rate_limit_is_refused(self):
        for value in (0, -1, "fast", True):
            with self.assertRaises(ValueError):
                OnlineExecutor(self.store, rate_limit=value)


if __name__ == "__main__":
    unittest.main()
