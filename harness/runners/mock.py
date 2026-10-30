"""确定性 MockRunner：按 task_id + prompt 规则返回，用于测试与离线演示。

它没有网络能力，但和真实 runner 走同一条预算、并发与审计路径：
每次调用都会计数 ``api_calls``，``external_api=False``，费用只记为模拟费用。
"""

import time

from .base import RunnerResult, prompt_text


class MockRunner:
    external_api = False

    def __init__(self, responder, *, unit_cost_cny: float = 0.0002, workers: int = 4,
                 model: str = "mock-deterministic", prompt_tokens: int = 120,
                 completion_tokens: int = 24, latency_s: float = 0.0):
        if not callable(responder):
            raise ValueError("MockRunner 需要 responder(task, prompt, artifacts) -> str")
        if unit_cost_cny < 0:
            raise ValueError("unit_cost_cny 必须非负")
        self.responder = responder
        self.unit_cost_cny = float(unit_cost_cny)
        self.workers = int(workers)
        self.model = model
        self.prompt_tokens = int(prompt_tokens)
        self.completion_tokens = int(completion_tokens)
        self.latency_s = float(latency_s)

    def build_messages(self, task, version, artifacts):
        return [dict(role="system", content=prompt_text(artifacts)),
                dict(role="user", content=task.question)]

    def reservation_amount(self, task, version, artifacts):
        return self.unit_cost_cny

    def invoke(self, task, version, artifacts) -> RunnerResult:
        began = time.monotonic()
        text = self.responder(task, prompt_text(artifacts), artifacts)
        if not isinstance(text, str):
            raise TypeError("MockRunner responder 必须返回字符串")
        if self.latency_s:
            time.sleep(self.latency_s)
        return RunnerResult(text=text, prompt_tokens=self.prompt_tokens,
                            completion_tokens=self.completion_tokens, cost_cny=0.0,
                            simulated_cny=self.unit_cost_cny,
                            latency_s=time.monotonic()-began, attempts=1,
                            external_api=False, usage_reported=True)

    def __call__(self, task, version, artifacts) -> str:
        result = self.invoke(task, version, artifacts)
        if result.error:
            raise RuntimeError(result.error)
        return result.text
