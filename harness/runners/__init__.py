"""Runner 实现：mock（确定性）与 OpenAI Chat Completions 兼容端点。"""

from .base import ModelRunner, RunnerResult, prompt_text
from .mock import MockRunner
from .openai_compatible import OpenAICompatibleRunner

__all__ = ["ModelRunner", "RunnerResult", "prompt_text", "MockRunner", "OpenAICompatibleRunner"]
