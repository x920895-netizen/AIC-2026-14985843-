"""反思提议器：读取失败轨迹与门禁理由，让 LLM 输出严格 JSON 的新指令。

约束：
- 只接收验证集失败轨迹（最多 8 条），永远不接触测试集；
- 输出必须含 ``diagnosis`` / ``change_summary`` / ``expected_gain`` /
  ``risk`` / ``new_prompt`` 五个字段；
- 新指令非空、不超长、必须与当前不同；任一项不满足会重试一次，
  再失败则返回 ``None``（本轮不提议，而不是硬塞一个候选）。
"""

import json

from .base import DEFAULT_COMPONENT, ProposalContext, prompt_proposal

REQUIRED_FIELDS = ("diagnosis", "change_summary", "expected_gain", "risk", "new_prompt")

SYSTEM = (
    "你是提示词改进助手。你在一个带发布门禁的自进化系统里工作："
    "你只能提出一条新的系统指令，是否采纳由独立门禁决定。"
    "只输出一个 JSON 对象，不要输出 Markdown 或解释。字段："
    "diagnosis（对失败原因的诊断，字符串）、change_summary（改了什么，字符串）、"
    "expected_gain（预期收益，字符串）、risk（风险，字符串）、new_prompt（新的完整系统指令，字符串）。"
)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


class ReflectiveProposer:
    name = "reflective"

    def __init__(self, store, client, *, component: str = DEFAULT_COMPONENT,
                 max_failures: int = 8, max_prompt_chars: int = 4000,
                 response_format: dict | None = None, attempts: int = 2):
        if not hasattr(client, "complete"):
            raise ValueError("反思提议器需要具备 complete(messages) 的客户端")
        self.store = store
        self.client = client
        self.component = component
        self.max_failures = int(max_failures)
        self.max_prompt_chars = int(max_prompt_chars)
        self.response_format = response_format or {"type": "json_object"}
        self.attempts = max(1, int(attempts))

    # ---- LLM 调用 ----

    def _ask(self, messages: list[dict]) -> str | None:
        try:
            result = self.client.complete(messages, response_format=self.response_format)
        except TypeError:
            try:
                result = self.client.complete(messages)
            except Exception:
                return None
        except Exception:
            return None
        if isinstance(result, str):
            return result
        error = getattr(result, "error", None)
        if error:
            return None
        text = getattr(result, "text", None)
        return text if isinstance(text, str) else None

    # ---- 解析与校验 ----

    @staticmethod
    def parse(text: str | None) -> dict | None:
        if not text:
            return None
        try:
            value = json.loads(_strip_fences(text))
        except (TypeError, ValueError):
            return None
        if not isinstance(value, dict):
            return None
        for name in REQUIRED_FIELDS:
            if not isinstance(value.get(name), str):
                return None
            # new_prompt 允许为空字符串，交给 validate_prompt 给出“不能为空”的具体理由。
            if name != "new_prompt" and not value[name].strip():
                return None
        return value

    def validate_prompt(self, prompt: str, current: str) -> str | None:
        prompt = prompt.strip()
        if not prompt:
            return "新指令不能为空"
        if len(prompt) > self.max_prompt_chars:
            return f"新指令超过 {self.max_prompt_chars} 字符"
        if prompt == current.strip():
            return "新指令与当前指令相同，没有实际变更"
        return None

    # ---- 主流程 ----

    def _messages(self, context: ProposalContext) -> list[dict]:
        failures = context.failures[:self.max_failures]
        lines = [f"轮次：{context.round_index}", "", "当前系统指令：", context.current_prompt,
                 "", f"最近失败样本（{len(failures)} 条，来自验证集）："]
        for i, case in enumerate(failures, 1):
            lines += [f"{i}. 问题：{case.question}",
                      f"   预测：{case.prediction or '(空)'}",
                      f"   参考：{case.gold}",
                      f"   EM={case.em:.2f} F1={case.f1:.2f}"]
        if not failures:
            lines.append("（本轮没有失败样本）")
        lines += ["", "最近一次门禁否决理由："]
        lines += [f"- {reason}" for reason in context.gate_reasons] or ["- 无"]
        lines += ["", "请只输出 JSON 对象。"]
        return [dict(role="system", content=SYSTEM), dict(role="user", content="\n".join(lines))]

    def propose(self, context: ProposalContext):
        messages = self._messages(context)
        current = context.current_prompt
        for _ in range(self.attempts):
            payload = self.parse(self._ask(messages))
            if payload is None:
                problem = "上一条输出不是合法 JSON 或缺少必需字段"
            else:
                problem = self.validate_prompt(payload["new_prompt"], current)
                if problem is None:
                    return prompt_proposal(
                        self.store, context, payload["new_prompt"].strip(),
                        basis=payload["diagnosis"].strip(),
                        expected_benefit=payload["expected_gain"].strip(),
                        risks=(payload["risk"].strip(),),
                        component=self.component)
            messages = messages+[dict(role="user", content=(
                f"上一次输出被拒绝：{problem}。请重新只输出一个 JSON 对象，"
                f"new_prompt 必须非空、不超过 {self.max_prompt_chars} 字符且与当前指令不同。"))]
        return None
