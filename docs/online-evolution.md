# 在线自进化闭环

v1.1 把“离线门禁原型”扩展成**可接真实模型**的闭环：提议器产生候选，执行器调用模型，门禁判定，事务发布或拒绝，每一步都落 SQLite 与凭证链。

## 数据流

```
失败轨迹(验证集) ──► Proposer(反思/预置) ──► CandidateProposal(冻结 GateSpec)
                                                    │
                                                    ▼
                                   OnlineExecutor ──► Runner ──► 模型端点
                                        │ 预留/结算预算     │ 429/5xx/超时重试
                                        ▼                   ▼
                                   runs/attempts  ◄──── 文本+usage+延迟
                                        │
                                        ▼
                            CandidateValidator(重算 EM/F1) ──► evaluate_gate
                                        │
                          accept(凭证) / reject(凭证) ──► current 前移或保持
```

关键约束：**测试集只在最后跑一次**，不参与候选选择；runner 只接收问题，不接收 gold；指标由评价器从持久化输出重算。

## 接入 OpenAI 兼容端点

任何兼容 `POST {base_url}/chat/completions` 的服务都可以：

```bash
export OPENAI_API_KEY=...            # 或自定义 --api-key-env
python -m harness.cli evolve \
  --db ./state \
  --dataset ./dataset.json \
  --proposer reflective \
  --runner openai \
  --base-url https://dashscope.aliyuncs.com/compatible-mode/v1 \
  --model qwen-plus \
  --input-price 0.8 --output-price 2.7 \
  --rounds 3 --budget 5 \
  --gate screen \
  --output evolve.json --report report.md
```

- `--input-price/--output-price` 是**元/百万 token**，用于把 usage 折算成人民币；不填则费用记为 0（预算不会被消耗，慎用）。
- `--json-mode` 会发送 `response_format={"type":"json_object"}`（反思提议器默认开启）。
- `--record-raw` 把原始响应写进运行记录；默认关闭，避免把大段模型输出落库。
- API Key 只从环境变量读取，不写入配置、日志或报告。

代码方式：

```python
from harness.runners.openai_compatible import OpenAICompatibleRunner

runner = OpenAICompatibleRunner(
    base_url="https://api.openai.com/v1", model="gpt-4o-mini",
    api_key_env="OPENAI_API_KEY", workers=4, max_retries=4,
    input_price_cny_per_million=0.8, output_price_cny_per_million=2.7)
```

## 成本控制

三层约束同时生效：

1. **预留**：每次调用前按 `输入字节上界 × 输入价 + max_tokens × 输出价` 预留；
2. **结算**：返回后按 usage 结算；没有 usage 时按预留上界记账并标 `usage_reported=false`；
3. **硬上限**：预算作用域 `limit_cny` 用尽后 `reserve` 抛 `BudgetExceeded`，运行标记 `stopped`，候选不会通过门禁。

`budgets` 文档同时记录 `simulated_cny` / `real_cny` / `actual_api_cost_cny` / `api_calls`，报告里分开呈现，**不会把模拟费用说成真实账单**。

## 并发

- 默认 4 个 worker，`--workers` 或 `runner.workers` 可配；`OnlineExecutor` 用 `ThreadPoolExecutor` 调度。
- 每个任务独立预留/结算；SQLite 用 `BEGIN IMMEDIATE` 串行化写操作（连接超时 30 秒）。
- 并发不改变结果：任务与输出按 `(repeat, task_id)` 排序后落库。
- 真实端点的速率限制由 runner 的 429 退避处理；不要靠加大并发去压测供应商。

## 断点续跑

有两层：

| 层级 | 入口 | 行为 |
| --- | --- | --- |
| 运行级 | `engine.run(..., resume_run_id=<run_id>)` / `OnlineExecutor` | 已完成 `(task_id, repeat)` 不再调用；失败/中断的行会被重新执行并替换 |
| 演化级 | `engine.resume(run_id, proposer, runner)` | 从**已提交轮次**继续：当前的版本、父运行、预算作用域、接受/拒绝计数都从 SQLite 恢复 |

演化级续跑的边界：每轮在 `candidate_evaluated` / `accepted` 事务提交后写入 `evolution_runs`，因此可以恢复到“第 N 轮已完成”。如果进程在**一轮中间**（例如候选已注册但还没验证）被杀，续跑会看到 `registered` 状态的候选；当前版本不提供自动接管，需要人工清理或换一个演化 ID。这是已知限制，不宣称任意时刻可恢复。

## 门禁模式

- `GateSpec.require_offline=True`：要求 `mode=simulation`、`api_calls=0`、真实费用 0。演示与单元测试用这一档。
- `GateSpec.require_offline=False`（默认）：允许真实运行；此时门禁额外校验 `attempts` 的 `api_calls`、真实费用与运行记录一致，防止“账本和结论各说各话”。

## 提议器

| 提议器 | 用途 | 说明 |
| --- | --- | --- |
| `MockProposer` | 测试/演示 | 预置 good_patch / bad_patch，不代表模型质量 |
| `ScriptedProposer` | 演示/复现 | 按 `round_index` 决定每轮策略，`None` 表示停止 |
| `ReflectiveProposer` | 真实闭环 | 最多 8 条失败轨迹 + 上一轮否决理由 → 严格 JSON；非法输出重试一次，仍失败返回 `None` |

反思提示只包含验证集失败样本，不包含测试集。

## 已知限制

- 只支持 Chat Completions 的文本补全；不支持流式、工具调用、多模态。
- 4xx（除 429）不重试，直接标记失败。
- 速率限制只做指数退避，不做跨进程配额协调。
- 成本按配置价目估算，实际账单可能因缓存/折扣不同。
- 真实大样本没有在评审环境跑过；本包只用本地 mock 端点做了端到端验证（见 `tests/test_online_runner.py` 与 `examples/evolve_demo.py`）。
