"""限流：令牌桶与 ``Retry-After`` 解析。

在线执行器按请求数限流（令牌桶），runner 在收到 429/503 时优先尊重服务端
给出的 ``Retry-After``，而不是盲目指数退避——两者都为了不把“被限流”变成
“把预算烧在重试上”。
"""

from __future__ import annotations

import threading
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

DEFAULT_MAX_RETRY_AFTER = 60.0


class RateLimitTimeout(RuntimeError):
    pass


class TokenBucket:
    """线程安全令牌桶：``rate`` 个/秒，容量默认与速率一致（1 秒突发）。"""

    def __init__(self, rate: float, burst: float | None = None, *, clock=time.monotonic,
                 sleep=time.sleep):
        rate = float(rate)
        if rate <= 0:
            raise ValueError("限流速率必须为正")
        burst = float(burst) if burst is not None else max(1.0, rate)
        if burst <= 0:
            raise ValueError("桶容量必须为正")
        self.rate = rate
        self.burst = burst
        self.clock = clock
        self.sleep = sleep
        self.tokens = burst
        self.updated = clock()
        self._lock = threading.Lock()
        self.acquired = 0
        self.total_wait_s = 0.0

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self.updated)
        if elapsed:
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.updated = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        tokens = float(tokens)
        if tokens <= 0:
            raise ValueError("令牌数必须为正")
        with self._lock:
            self._refill(self.clock())
            if self.tokens + 1e-12 >= tokens:
                self.tokens -= tokens
                self.acquired += 1
                return True
            return False

    def acquire(self, tokens: float = 1.0, *, timeout: float | None = None) -> float:
        """阻塞直到拿到令牌，返回累计等待秒数；超时抛 ``RateLimitTimeout``。"""
        tokens = float(tokens)
        if tokens <= 0:
            raise ValueError("令牌数必须为正")
        if tokens > self.burst:
            raise ValueError("单次请求令牌数不能超过桶容量")
        waited = 0.0
        while True:
            with self._lock:
                now = self.clock()
                self._refill(now)
                if self.tokens + 1e-12 >= tokens:
                    self.tokens -= tokens
                    self.acquired += 1
                    self.total_wait_s += waited
                    return waited
                delay = max(1e-9, (tokens - self.tokens) / self.rate)
            if timeout is not None and waited + delay > timeout:
                raise RateLimitTimeout(f"等待令牌超时（已等待 {waited:.3f}s）")
            self.sleep(delay)
            waited += delay

    def stats(self) -> dict:
        with self._lock:
            self._refill(self.clock())
            return dict(rate=self.rate, burst=self.burst, tokens=round(self.tokens, 6),
                        acquired=self.acquired, total_wait_s=round(self.total_wait_s, 6))


def parse_retry_after(value, *, now: datetime | None = None,
                      cap: float = DEFAULT_MAX_RETRY_AFTER) -> float | None:
    """解析 ``Retry-After``：秒数或 HTTP-date；无效值返回 ``None``。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            moment = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if moment is None:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        seconds = (moment - current).total_seconds()
    if seconds != seconds or seconds < 0:  # NaN 或负数
        return None
    return min(float(seconds), float(cap))
