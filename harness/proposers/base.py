"""提议器协议：失败轨迹 + 门禁理由 → 一个冻结父版本的候选。"""

from dataclasses import dataclass
from typing import Protocol

from ..contracts import CandidateProposal, GateSpec, TargetKind

DEFAULT_COMPONENT = "final_answer.predict"


@dataclass(frozen=True)
class FailureCase:
    task_id: str
    question: str
    prediction: str
    gold: str
    em: float
    f1: float

    def to_dict(self) -> dict:
        return dict(task_id=self.task_id, question=self.question, prediction=self.prediction,
                    gold=self.gold, em=self.em, f1=self.f1)


@dataclass
class ProposalContext:
    """一轮提议需要的全部上下文；不含测试集内容。"""

    parent_version: dict
    artifacts: dict
    gate: GateSpec
    failures: tuple[FailureCase, ...] = ()
    gate_reasons: tuple[str, ...] = ()
    round_index: int = 0

    @property
    def current_prompt(self) -> str:
        components = (self.artifacts.get("prompt") or {}).get("components") or {}
        return components.get(DEFAULT_COMPONENT) or next(iter(components.values()), "")


class Proposer(Protocol):
    name: str

    def propose(self, context: ProposalContext) -> CandidateProposal | None: ...


def prompt_proposal(store, context: ProposalContext, new_prompt: str, *, basis: str,
                    expected_benefit: str, risks: tuple[str, ...] = (),
                    component: str = DEFAULT_COMPONENT) -> CandidateProposal:
    """把一段新指令写成 Prompt 工件并返回候选提议。"""
    current = (context.artifacts.get("prompt") or {}).get("components") or {}
    target = component if component in current else next(iter(sorted(current)), component)
    components = dict(current)
    components[target] = new_prompt
    ref = store.write_artifact("prompt", {"components": components})
    return CandidateProposal(
        parent_version_id=context.parent_version["version_id"],
        target=TargetKind.PROMPT,
        artifact_ref=ref,
        gate=context.gate,
        basis=basis,
        expected_benefit=expected_benefit or "待离线/在线验证",
        risks=tuple(risks),
    )
