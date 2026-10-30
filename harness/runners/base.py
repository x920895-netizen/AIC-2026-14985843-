"""Runner 协议与统一结果对象。

Runner 负责“一个任务 → 一段文本”，不接触参考答案；评分始终由
``harness.evaluation`` 从持久化输出重算。
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass
class RunnerResult:
    """一次任务执行的结果（真实或 mock）。"""

    text: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: 真实费用（external_api=True 时由 usage 计算）
    cost_cny: float = 0.0
    #: 模拟费用（external_api=False 时计入预算）
    simulated_cny: float = 0.0
    latency_s: float = 0.0
    attempts: int = 1
    external_api: bool = False
    usage_reported: bool = True
    raw: dict | None = None
    error: str | None = None


class ModelRunner(Protocol):
    external_api: bool
    workers: int

    def invoke(self, task, version: dict, artifacts: dict) -> RunnerResult: ...

    def build_messages(self, task, version: dict, artifacts: dict) -> list[dict]: ...

    def reservation_amount(self, task, version: dict, artifacts: dict) -> float: ...


def prompt_text(artifacts: dict) -> str:
    """把 Prompt 工件的组件按名称拼接成系统提示。"""
    components = (artifacts.get("prompt") or {}).get("components") or {}
    return "\n\n".join(f"{name}: {text}" for name, text in sorted(components.items()))
