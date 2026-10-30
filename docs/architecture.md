# 架构

## 设计目标

不是再造优化器，而是把“候选—证据—判定—发布—回滚”变成可复核的协议。四条硬约束：

1. **证据不可事后变形**：候选注册时冻结工件 diff、任务清单、评价器与模型配置；验证时重算，不采信调用方给的均分。
2. **判定与发布分离**：`validate` 只产出报告与决策，`accept` 才切换当前版本，且在同一事务内复核。
3. **拒绝也要留痕**：任何否决都写决策与凭证，理由逐条持久化。
4. **在线与离线共用同一条路径**：执行器分层，但预算、状态、门禁、凭证只有一套。

## 模块

| 模块 | 职责 | 网络/模型 |
| --- | --- | --- |
| `contracts.py` | `GateSpec`（两级预设、`require_offline`）、`CandidateProposal`、工件 schema、规范化 JSON 与 SHA-256 | 否 |
| `gates.py` | 两级门禁纯函数（`evaluate_gate`）与三态判定（`assess_gate`：pass / fail / insufficient） | 否 |
| `stats.py` | Wilson、精确 McNemar（含连续性校正）、百分位/BCa bootstrap、MDE、power | 否 |
| `selection.py` | 选择偏差：比较次数、有效比较、候选数 > 3 的族错误率警告 | 否 |
| `protocol.py` | `ProtocolSpec` 冻结与导入导出、`TestSetGuard`（测试集只跑一次） | 否 |
| `trace.py` | JSONL 事件日志（trace/1），固定六字段、过滤、摘要 | 否 |
| `ratelimit.py` | 令牌桶与 `Retry-After` 解析 | 否 |
| `evaluation.py` | 逐题重算 EM/F1、配对 bootstrap、调用 `evaluate_gate`；EM 为本地 HotpotQA 规范化实现 | 否 |
| `receipt.py` | 凭证构造、哈希链写入、`verify_chain` | 否 |
| `storage.py` | SQLite 文档表（WAL）、事务、原子工件文件、版本封印、祖先回滚 | 否 |
| `execution.py` | `Executor` 协议；`OfflineExecutor`（无发送能力）与 `OnlineExecutor`（并发、预留/结算、断点续跑） | 取决于 runner |
| `runners/base.py` | `RunnerResult`、`ModelRunner` 协议、Prompt 渲染 | 否 |
| `runners/mock.py` | 确定性 MockRunner（`api_calls` 计数、`external_api=False`） | 否 |
| `runners/openai_compatible.py` | Chat Completions 兼容客户端：429/5xx/超时退避、usage 计费、JSON 模式 | 是（真实端点） |
| `proposers/base.py` | `FailureCase`、`ProposalContext`、候选构造 | 否 |
| `proposers/mock.py` | 预置 good/bad 补丁与按轮次脚本 | 否 |
| `proposers/reflective.py` | 失败轨迹 → 严格 JSON → 新指令；重试一次 | 需要一个 LLM 客户端 |
| `budget.py` | 预算作用域、预留/结算、模拟与真实费用分账 | 否 |
| `experience.py` | 经验生命周期与检索 | 否 |
| `controller.py` | 生命周期编排 + `evolve()` 在线闭环 + `resume(run_id)` | 否（调用执行器） |
| `report.py` | 从 SQLite 生成 Markdown 报告 | 否 |
| `cli.py` | 命令行入口（含 `stats` / `trace` / `protocol-export|import|show`） | 按命令 |

## 状态机

```
                       ┌──────────── reject（门禁否决 / 显式拒绝）
                       ▼
registered ──validate──► validated ──accept──► accepted
                       │
                       └──► rejected
```

- `register` 只接受基于**当前已接受版本**的提议；父版本过期直接拒绝；验证集可以是数据集的子集，但顺序必须与数据集一致。
- `validate` 后候选状态为 `validated`（门禁通过）或 `rejected`（门禁否决）。
- `accept` 要求状态为 `validated`，并复核：父/候选版本内容哈希、重算指标与报告一致、预算作用域无未决预留。
- `rollback` 只允许回到**当前版本的祖先**，且目标必须仍是 `accepted`。

### 演化闭环

```
基线版本 ──run(val)──► 失败轨迹 ──propose──► candidate ──run(val)──► validate
   ▲                                                                   │
   │                                       accept（current 前移）◄─────┤
   │                                       reject（current 不变）◄─────┘
   └── 停止：轮次用尽 / 连续 2 次拒绝 / 预算耗尽 / 提议器放弃
                                                       │
                                                       ▼
                                        run(test) 基线与最终版本各一次（不参与选择）
```

每轮提交后写 `evolution_runs` 文档（轮次、候选、决策、指标、预算作用域、父运行），因此可以从**已提交轮次**续跑。

## 持久化

单个 SQLite 文件 `foundation.sqlite3`：

| 表 | 用途 |
| --- | --- |
| `documents(collection,id,payload,digest)` | 所有实体：datasets/versions/candidates/runs/evaluations/decisions/experiences/budgets/attempts/**evolution_runs** |
| `meta(key,value)` | `current_version` 指针 |
| `events(seq,kind,payload,created_at)` | 追加式事件流（含每轮 `evolution_round`） |
| `receipts(seq,receipt_id,candidate_id,decision,payload,digest)` | 决策凭证链；`(candidate_id,decision)` 唯一 |

运行记录（`runs`）在 v1.1 增加：`executor`、`workers`、`external_api`、`runner`、`mode=real|simulation`、`api_calls`、`actual_api_cost_cny`，以及每条输出的 `prompt_tokens`/`completion_tokens`/`attempts`/`usage_reported`/`cost_overrun`。

预算作用域（`budgets`）在 v1.1 增加 `simulated_cny`、`real_cny`，与 `accounted_cny` 并列展示。

## 不变量

1. `current_version` 永远指向一个 `accepted` 版本。
2. 版本内容哈希覆盖除 `content_hash` 外的全部字段；工件路径不得越出 `artifacts/`。
3. 同一父版本 + 同一 diff + 同一验证条件的候选只能登记一次（防重复付费）。
4. 运行记录的 `condition_hash` 必须与冻结的 `GateSpec` 一致。
5. `require_offline=True` 时，运行必须是 `simulation`、零调用、零真实费用。
6. 真实运行的 `api_calls` 与真实费用必须等于其结算槽位之和（账本与结论同源）。
7. 凭证的 `prev_receipt_hash` 必须等于上一条 `receipt_hash`，首条为 `""`。
8. 测试集的运行只在闭环结束后发生，且 `test_metrics.selection_used_test = False`。

## 为什么用 SQLite 而不是 JSON 文件

事务与唯一约束是“原子接受”“拒绝重复提议”“凭证链唯一”的实现基础。文件方案需要自己重写锁、崩溃恢复和唯一性检查，且很难证明 `accept` 不会留下“版本已切换但指针未切换”的中间态。
