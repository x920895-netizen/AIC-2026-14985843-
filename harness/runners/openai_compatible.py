"""OpenAI Chat Completions 兼容 runner（任意 base_url）。

- 429 / 5xx / 超时：指数退避重试，最多 ``max_retries`` 次（默认 4）；
- 429/503 且带 ``Retry-After`` 时优先按服务端给出的等待时间重试；
- 可选令牌桶限流（``rate_limiter``），与执行器级限流二选一或叠加；
- 每次调用返回 token 用量、延迟、尝试次数与可选原始响应；
- 无 usage 时按预留上界记账并标记 ``usage_reported=False``；
- 不内置并发：并发由 :class:`harness.execution.OnlineExecutor` 按
  ``runner.workers`` 调度（默认 4）。
"""

import http.client
import json
import os
import socket
import time
import urllib.error
import urllib.request

from scripts.usage import estimate_reservation
from .base import RunnerResult, prompt_text
from ..ratelimit import parse_retry_after

RETRYABLE = {429, 500, 502, 503, 504}
#: 明确支持 ``Retry-After`` 的状态码。
RETRY_AFTER_STATUSES = {429, 503}


def _header(headers, name: str) -> str | None:
    """大小写不敏感的响应头查找。"""
    if not headers:
        return None
    lowered = {str(k).lower(): v for k, v in dict(headers).items()}
    return lowered.get(name.lower())


class OpenAICompatibleRunner:
    external_api = True

    def __init__(self, *, base_url: str, model: str, api_key: str | None = None,
                 api_key_env: str = "OPENAI_API_KEY", workers: int = 4,
                 max_retries: int = 4, timeout: float = 60.0, retry_backoff: float = 0.5,
                 temperature: float = 0.0, max_tokens: int = 1024,
                 response_format: dict | None = None, record_raw: bool = False,
                 input_price_cny_per_million: float = 0.0,
                 output_price_cny_per_million: float = 0.0,
                 system_prompt: str | None = None, opener=None, sleep=time.sleep,
                 rate_limiter=None, max_retry_after: float = 60.0):
        if not base_url or not model:
            raise ValueError("OpenAI 兼容 runner 需要 base_url 与 model")
        if max_retries < 1:
            raise ValueError("max_retries 至少为 1")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.api_key_env = api_key_env
        self.workers = int(workers)
        self.max_retries = int(max_retries)
        self.timeout = float(timeout)
        self.retry_backoff = float(retry_backoff)
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.response_format = response_format
        self.record_raw = record_raw
        self.input_price = float(input_price_cny_per_million)
        self.output_price = float(output_price_cny_per_million)
        self.system_prompt = system_prompt
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep
        self.rate_limiter = rate_limiter
        self.max_retry_after = float(max_retry_after)
        self.last_retry_after: float | None = None
        self.retry_after_honored = 0

    # ---- 配置 ----

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return self.base_url+"/chat/completions"

    def api_key_value(self) -> str:
        return self.api_key or os.environ.get(self.api_key_env, "")

    # ---- 输入构造 ----

    def build_messages(self, task, version, artifacts) -> list[dict]:
        system = prompt_text(artifacts)
        if self.system_prompt:
            system = f"{self.system_prompt}\n\n{system}".strip()
        return [dict(role="system", content=system), dict(role="user", content=task.question)]

    def estimate_messages_cost(self, messages: list[dict]) -> float:
        return estimate_reservation(messages, self.max_tokens, [self.input_price, self.output_price])

    def reservation_amount(self, task, version, artifacts) -> float:
        return max(self.estimate_messages_cost(self.build_messages(task, version, artifacts)), 1e-9)

    # ---- 调用 ----

    def complete(self, messages: list[dict], *, response_format: dict | None = None) -> RunnerResult:
        key = self.api_key_value()
        if not key:
            return RunnerResult(external_api=True, attempts=0,
                                error=f"缺少 API Key：请设置环境变量 {self.api_key_env}")
        payload = dict(model=self.model, messages=messages, temperature=self.temperature,
                       max_tokens=self.max_tokens)
        fmt = response_format or self.response_format
        if fmt:
            payload["response_format"] = fmt
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
        last_error = ""
        for attempt in range(1, self.max_retries+1):
            began = time.monotonic()
            if self.rate_limiter is not None:
                self.rate_limiter.acquire()
            retry_after = None
            try:
                # 注意：响应头必须用独立变量接收，否则会在重试时把响应头当成请求头，
                # 丢掉 Authorization 并带上上一次响应的 Content-Length。
                status, body, response_headers = self._post(data, headers)
                if 200 <= status < 300:
                    return self._parse(body, attempt, messages, time.monotonic()-began)
                last_error = f"HTTP {status}: {body[:200]}"
                if status in RETRY_AFTER_STATUSES:
                    retry_after = parse_retry_after(
                        _header(response_headers, "retry-after"), cap=self.max_retry_after)
                if status not in RETRYABLE and status < 500:
                    return RunnerResult(external_api=True, attempts=attempt, error=last_error,
                                        latency_s=time.monotonic()-began)
            except (socket.timeout, TimeoutError) as exc:
                last_error = f"超时（{self.timeout}s）：{exc or type(exc).__name__}"
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", exc)
                if isinstance(reason, (socket.timeout, TimeoutError)):
                    last_error = f"超时（{self.timeout}s）：{reason}"
                else:
                    last_error = f"连接失败：{reason}"
            except (http.client.HTTPException, ConnectionError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < self.max_retries:
                if retry_after is not None:
                    self.last_retry_after = retry_after
                    self.retry_after_honored += 1
                    self._sleep(retry_after)
                else:
                    self._sleep(min(self.retry_backoff*(2**(attempt-1)), 8.0))
        return RunnerResult(external_api=True, attempts=self.max_retries,
                            error=f"重试 {self.max_retries} 次仍失败：{last_error}")

    def _post(self, data: bytes, headers: dict) -> tuple[int, str, dict]:
        request = urllib.request.Request(self.endpoint, data=data, headers=headers, method="POST")
        try:
            with self._opener(request, timeout=self.timeout) as response:
                return (int(getattr(response, "status", 200)), response.read().decode("utf-8"),
                        dict(getattr(response, "headers", {}) or {}))
        except urllib.error.HTTPError as exc:
            return (int(exc.code), (exc.read() or b"").decode("utf-8", errors="replace"),
                    dict(exc.headers or {}))

    def _parse(self, body: str, attempt: int, messages: list[dict], latency: float) -> RunnerResult:
        try:
            data = json.loads(body)
            choice = (data.get("choices") or [{}])[0]
            text = (choice.get("message") or {}).get("content")
            if not isinstance(text, str):
                raise ValueError("响应缺少 choices[0].message.content")
        except (ValueError, TypeError, AttributeError) as exc:
            return RunnerResult(external_api=True, attempts=attempt, latency_s=latency,
                                error=f"响应解析失败：{exc}")
        usage = data.get("usage") or {}
        if usage.get("prompt_tokens") is not None or usage.get("completion_tokens") is not None:
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            cost = (prompt_tokens*self.input_price+completion_tokens*self.output_price)/1e6
            return RunnerResult(text=text, prompt_tokens=prompt_tokens,
                                completion_tokens=completion_tokens, cost_cny=cost,
                                latency_s=latency, attempts=attempt, external_api=True,
                                usage_reported=True,
                                raw=data if self.record_raw else None)
        # 无 usage：按预留上界记账，明确标记，避免把缺失用量当成零成本。
        return RunnerResult(text=text, prompt_tokens=0, completion_tokens=0,
                            cost_cny=self.estimate_messages_cost(messages),
                            latency_s=latency, attempts=attempt, external_api=True,
                            usage_reported=False, raw=data if self.record_raw else None)

    def invoke(self, task, version, artifacts) -> RunnerResult:
        if not self.api_key_value():
            return RunnerResult(external_api=True, attempts=0,
                                error=f"缺少 API Key：请设置环境变量 {self.api_key_env}")
        try:
            return self.complete(self.build_messages(task, version, artifacts))
        except Exception as exc:  # 任何未预期异常都不能静默通过
            return RunnerResult(external_api=True, error=f"{type(exc).__name__}: {exc}")

    def __call__(self, task, version, artifacts) -> str:
        result = self.invoke(task, version, artifacts)
        if result.error:
            raise RuntimeError(result.error)
        return result.text
