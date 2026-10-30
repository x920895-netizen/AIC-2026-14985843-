# 门禁规范

## 两档预设

| 参数 | `screen`（探索筛查） | `release`（发布门禁） |
| --- | ---: | ---: |
| `min_em_gain` | 0.02（≥2pp） | 0.02（≥2pp） |
| `max_f1_drop` | 0.02（≤2pp） | 0.01（≤1pp） |
| `max_regression_rate` | 0.10（≤10%） | 0.05（≤5%） |
| `max_cost_ratio` | 2.0 | 1.2 |
| `require_positive_interval` | False | True |

`release` 与原型历史默认值完全一致，因此 `GateSpec(dataset_id, task_ids, model_config)` 的行为没有变化——加入两级门禁是**向后兼容**的。

```python
from harness import GateSpec

spec = GateSpec.release(dataset, task_ids, model_config)   # 默认档
spec = GateSpec.screen(dataset, task_ids, model_config)    # 探索档
spec = GateSpec.screen(dataset, task_ids, model_config, max_cost_ratio=3.0)  # 覆盖参数 → profile 记为 custom
```

`gate_profile(spec)` 从阈值反推档位：与预设完全一致返回 `screen` / `release`，否则返回 `custom`，不会把改过阈值的实验冒充成预设。

## 判定函数

```python
from harness.gates import evaluate_gate

passed, reasons = evaluate_gate(metrics, spec)
```

纯函数，只读 `metrics` 与 `spec`，不触碰存储、时间或随机数。`CandidateValidator` 在重算指标后调用它，二者不会出现两套判据。

`metrics` 需要的字段：

| 字段 | 含义 |
| --- | --- |
| `delta_em` | 候选 EM − 基线 EM |
| `delta_f1` | 候选 F1 − 基线 F1 |
| `regression_rate` | 回归题数 / 原正确题数；无原正确题时为 `None` |
| `interval` | 配对 bootstrap 95% 区间 `[lo, hi]` |
| `cost_ratio` 或 `parent_cost_cny` + `candidate_cost_cny` | 推理成本比 |

## 否决理由（固定中文措辞）

| 触发条件 | 理由 |
| --- | --- |
| `delta_em + 1e-12 < min_em_gain` | `主指标EM增益不足` |
| `delta_f1 < -max_f1_drop - 1e-12` | `辅助指标F1退化超限` |
| `regression_rate > max_regression_rate + 1e-12` | `逐题回归率超限` |
| `cost_ratio > max_cost_ratio + 1e-12` | `候选推理成本比超限` |
| `require_positive_interval and interval[0] <= 0` | `配对区间不足以支持正增益` |

顺序固定，便于日志比对与凭证复核。

## 历史回放（R1 / R2）

`examples/gate_replay.py` 只读 `evidence/historical_r1.json`、`historical_r2.json` 的计数与指标，用与 `CandidateValidator` 相同的配对 bootstrap（`random.Random(1)`，2000 次，百分位索引 49 / 1949）重算区间，再交给 `evaluate_gate`。

| 实验 | 档位 | 决策 | EM 变化 | F1 变化 | 回归率 | 成本比 | 配对区间 | 否决理由 |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| R1 | screen | **PASS** | +13.33pp | +11.92pp | 0.00% | 1.6477 | [+3.33, +26.67] | — |
| R1 | release | **FAIL** | +13.33pp | +11.92pp | 0.00% | 1.6477 | [+3.33, +26.67] | 候选推理成本比超限 |
| R2 | screen | **FAIL** | −6.67pp | −1.10pp | 8.70% | 1.1306 | [−16.67, 0.00] | 主指标EM增益不足 |
| R2 | release | **FAIL** | −6.67pp | −1.10pp | 8.70% | 1.1306 | [−16.67, 0.00] | 主指标EM增益不足；辅助指标F1退化超限；逐题回归率超限；配对区间不足以支持正增益 |

读法：

- **R1 值得继续，但不能发布**：EM 提升 13.33pp、零回归、区间下界 +3.33pp，探索筛查放行；发布门禁只因为推理成本比 1.6477 > 1.2 拦住它。这说明两档门禁确实在回答不同问题。
- **R2 两个档位都该拦**：EM 倒退 6.67pp、回归率 8.70% 超过发布线，发布门禁命中 4 条理由。若只有“EM 是否提升”一个指标，这条候选会在某些口径下被误当成“差不多”；回归率与区间把退化显式暴露出来。

## 成本口径

- R1：测试阶段 API 费用估算 ¥0.1591197 → ¥0.2621799，比值 1.647690。
- R2：汇总未拆分单题费用，成本比用**每题平均延迟**代理（21.2449s → 24.0189s，比值 1.130573）。这是代理指标，不是账单金额；`evidence/historical_r2.json` 的 `cost.kind` 已标注 `per_task_latency_proxy`。

## 接入自己的指标

`evaluate_gate` 只依赖上表字段，不与 HotpotQA 绑定。接入新任务时：

1. 用自己的评价器计算 `delta_em` 等字段；
2. 仍然调用 `evaluate_gate`，或复制两档阈值；
3. 若评价协议不同（例如校园场景的 `campus-policy-v1`），通过 `GateSpec(evaluator_id=...)` 指定；`EVALUATOR_IDS` 是白名单，调用方不能自定义打分器绕过门禁。
