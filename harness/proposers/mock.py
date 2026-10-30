"""预置提议器：good_patch / bad_patch，用于端到端演示与测试。"""

from .base import DEFAULT_COMPONENT, ProposalContext, prompt_proposal

PATCHES = {
    "good_patch": ("先逐跳确定问题中的主语实体，再核对最终答案的完整名称、限定词与单位；"
                   "只输出最终答案，不要解释。"),
    "good_patch2": ("先逐跳确定问题中的主语实体，并复述依据的原文片段，再核对最终答案的"
                    "完整名称、限定词与单位；只输出最终答案，不要解释。"),
    "bad_patch": ("回答尽量简短，可以省略限定词和单位；不确定时优先猜测最常见的答案。"),
    "bad_patch2": ("回答尽量简短，优先猜测最常见答案并省略单位；不确定时直接给出猜测，不要核对原文。"),
    "small_patch": ("只检查前两跳并套用模板；跳过最终答案的限定词核对，直接输出答案。"),
}


class MockProposer:
    """固定补丁提议器；每次都会追加轮次标记，保证产物与当前版本不同。"""

    name = "mock"

    def __init__(self, store, strategy: str = "good_patch", *, component: str = DEFAULT_COMPONENT):
        if strategy not in PATCHES:
            raise ValueError(f"未知预置策略：{strategy}（可选 {sorted(PATCHES)}）")
        self.store = store
        self.strategy = strategy
        self.component = component

    def propose(self, context: ProposalContext):
        prompt = PATCHES[self.strategy]+f"\n<!-- {self.strategy} / round {context.round_index} -->"
        return prompt_proposal(self.store, context, prompt,
                               basis=f"预置策略 {self.strategy}（第 {context.round_index} 轮）",
                               expected_benefit="预置补丁：用于验证门禁与事务路径",
                               risks=(f"{self.strategy} 不代表真实模型反思质量",),
                               component=self.component)


class ScriptedProposer:
    """按轮次脚本返回策略；``None`` 表示本轮不再提议。

    轮次索引来自 ``context.round_index``，因此断点续跑时不需要重放前面的轮次。
    """

    name = "scripted"

    def __init__(self, store, steps, *, component: str = DEFAULT_COMPONENT):
        self.store = store
        self.steps = list(steps)
        self.component = component

    def propose(self, context: ProposalContext):
        index = context.round_index
        strategy = self.steps[index] if index < len(self.steps) else None
        if strategy is None:
            return None
        return MockProposer(self.store, strategy, component=self.component).propose(context)
