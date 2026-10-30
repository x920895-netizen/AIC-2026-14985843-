# 循证演化：自进化智能体的发布门禁

> 自进化智能体不缺候选，缺的是“哪一个候选配得上发布”的证据与门禁。

面向自进化智能体、提示词与工件迭代的**开源评测与发布治理工具**。它不生产新的优化器，而是把“改—测—判—发—回滚”做成可冻结、可复核、可回滚的协议：

- **证据绑定**：候选 = 工件 diff + 冻结的数据清单 + 冻结的评价器 + 冻结的模型与预算；
- **两级门禁**：`screen`（探索筛查，值不值得继续花钱）与 `release`（发布门禁，能不能上线）；
- **统计口径**：Wilson 区间、精确 McNemar（含连续性校正）、BCa bootstrap、MDE；样本不足输出第三态“证据不足”；
- **事务发布**：候选在单个 SQLite 事务里接受并切换指针，可回滚到任一祖先版本；
- **决策凭证**：每次 accept / reject 落一条 SHA-256 哈希链凭证，绑定 `protocol_hash`，可离线复核是否被改动；
- **在线闭环**：执行器分层（离线 / 在线），可接任意 OpenAI 兼容端点；反思提议器把失败轨迹变成新候选，验证集选择、测试集只跑一次。

## 真实结果（唯一口径）

HotpotQA-Mini，固定种子 1，30/30/30 划分；R1 为真实 API 优化结果，R2 为新独立 30 题上的负面结果。

| 实验 | 题数 | 基线 EM | 候选 EM | 变化 | 回归 |
| --- | ---: | ---: | ---: | ---: | ---: |
| R1 独立测试 | 30 | 63.33% | 76.67% | +13.33pp | 0/23 |
| R2 新独立测试 | 30 | 76.67% | 70.00% | −6.67pp | 2/23 |

- R1 的 F1 从 70.23% 升到 82.15%，但配对区间为 [+3.33pp, +26.67pp]，**精确 McNemar p=0.125，不宣称统计显著**。
- R2 的 F1 从 82.00% 降到 80.90%，回归率 2/23 = 8.70%，**作为负面结果完整保留**，没有据此回选候选。
- 同一组数字喂给两级门禁：R1 通过 `screen`、被 `release` 仅以“成本比 1.65 > 1.2”拦下；R2 在 `screen` 与 `release` 都被否决（`release` 命中 4 条理由）。可用 `python examples/gate_replay.py` 复算。
- 早期材料中出现过的合成预测成绩与“多次运行稳定性”情景数字，均为脚本产物或人工设定，**已从本仓库彻底移除**；本仓库不含任何此类数字，也不得再引用。

## 30 秒快速开始

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

# 顺序很重要：先装仓库内的 GEPA，再装 research 依赖
pip install ./upstream/gepa
pip install -e ".[research]"

pytest tests/                        # 全部离线，不需要 API Key
python examples/gate_replay.py --output evidence/gate_replay.json
python scripts/demo_submission.py --output demo-output-01
python examples/evolve_demo.py       # 两个场景：基本闭环 + 5 候选选择偏差（凭证链校验）
python examples/bench_write.py       # 可选：SQLite 写入压测（1000 条，约 30–60 秒）
```

安装本身需要几分钟并联网一次；之后所有验证都不联网、不产生费用。

> **不要跳过 `pip install ./upstream/gepa`。** PyPI 上的 `gepa 0.1.4` 版本号相同，但缺少 `best_score`，会导致测试 32 passed / 1 failed。细节见 [docs/quickstart.md](docs/quickstart.md)。

## 架构

```
harness/
├── contracts.py    契约与校验：GateSpec（含 screen/release 预设、min_sample）、工件 schema、规范化哈希
├── gates.py        两级门禁纯函数 evaluate_gate + 三态 assess_gate（pass / fail / insufficient）
├── stats.py        统计口径：Wilson、精确 McNemar、百分位/BCa bootstrap、MDE、power
├── selection.py    选择偏差：比较次数、有效比较、候选数 > 3 的族错误率警告
├── protocol.py     ProtocolSpec：划分/评价器/阈值/种子冻结、导入导出、测试集只跑一次守卫
├── trace.py        JSONL 事件日志（trace/1）：固定字段、过滤、摘要、抗损坏
├── ratelimit.py    令牌桶限流与 Retry-After 解析
├── evaluation.py   独立评价器：从持久化原始输出重算 EM/F1、配对区间与统计量
├── receipt.py      决策凭证：哈希链写入、verify_chain(conn) 复核
├── storage.py      SQLite 事务状态（WAL）、原子工件写入、祖先回滚
├── execution.py    执行器分层：OfflineExecutor（无发送能力）/ OnlineExecutor（并发、预留、续跑）
├── runners/        mock 与 OpenAI 兼容 runner（429/5xx/超时退避、Retry-After、usage 计费、JSON 模式）
├── proposers/      mock 预置补丁与 reflective 反思提议器（严格 JSON、失败关闭）
├── budget.py       预算作用域与预留/结算，模拟费用与真实费用分账
├── experience.py   经验生命周期（模拟验证不升级为真实证据）
├── controller.py   生命周期协调器 + evolve() 在线闭环 + resume(run_id) 断点续跑
├── report.py       从 SQLite 生成 Markdown 报告
└── cli.py          命令行：init / run / evolve / stats / trace / protocol-* / report / inspect / receipts / verify-receipts / rollback
```

一次完整的候选流程：

1. `register` 冻结候选的 diff、验证清单、评价器与模型配置，重复提议直接拒绝；
2. `run` 在预算作用域内逐题执行，每次执行都预留并结算；
3. `validate` 从原始输出**重算**指标，交给 `evaluate_gate` 判定；否决理由逐条持久化；
4. `accept` 在同一事务内复核版本哈希、重算指标、切换 current 指针并写入 accept 凭证；
5. `reject` 或门禁否决写入 reject 凭证；`rollback` 只能回到当前版本的祖先。

## 在线闭环（v1.1）

```python
from harness import EvolutionFoundation
from harness.runners.openai_compatible import OpenAICompatibleRunner
from harness.proposers.reflective import ReflectiveProposer

runner = OpenAICompatibleRunner(base_url="https://api.openai.com/v1", model="gpt-4o-mini",
                                api_key_env="OPENAI_API_KEY", workers=4,
                                input_price_cny_per_million=0.8, output_price_cny_per_million=2.7)
result = engine.evolve("my-set", val_ids, test_ids,
                       ReflectiveProposer(store, runner), runner,
                       gate_profile="screen", max_rounds=3, budget_cny=5.0)
```

一轮之内：当前版本失败轨迹（最多 8 条）→ 提议器输出严格 JSON → 候选冻结 GateSpec → 同验证集运行 → 重算指标 → `evaluate_gate` → accept（写凭证、current 前移）或 reject（保留父版本）。停止条件：轮次用尽、连续 2 次拒绝、预算耗尽、提议器放弃。结束后**只在测试集**给基线与最终版本各跑一次，`test_metrics.selection_used_test=false`。

- 预算：每次调用前预留、返回后按 usage 结算；无 usage 按预留上界记账并标记 `usage_reported=false`；超限即停。
- 并发：`ThreadPoolExecutor`，默认 4 worker，可配。
- 续跑：运行级（已完成 `(task_id, repeat)` 不重复调用）与演化级（`engine.resume(run_id, proposer, runner)` 从已提交轮次继续）。
- 离线演示：`python examples/evolve_demo.py` 用 MockRunner 跑两个场景（A：3 轮，第 1 轮拒绝、第 2 轮接受、第 3 轮不再提议；B：5 个候选触发选择偏差警告），约 10 秒完成。

**真实边界**：代码路径只用**本地 mock 端点**做过端到端验证（`tests/test_online_runner.py` 起本地 `http.server` 覆盖 429/500/超时/usage 缺失/8 路并发/续跑）；**没有在评审环境用真实模型跑新的大样本**。历史 R1/R2 来自外部研究脚本（GEPA），与本闭环的 mock 结果不是同一回事，不能混为一谈。

## 统计口径（v1.2）

门禁只认“能复算的量”。所有统计函数在 `harness/stats.py`，纯计算、无网络，
随机性只来自显式种子；完整公式与校验值见 [docs/statistics.md](docs/statistics.md)。

| 量 | 实现 | 口径要点 |
| --- | --- | --- |
| 比例区间 | `wilson_interval` | Wilson score，不用 Wald；`n=0` 返回 [0,1] 并置 `point=null` |
| 配对显著性 | `mcnemar_exact` | 默认精确二项（双侧）；`continuity=True` 用 `(|b-c|-1)²/(b+c)` 卡方近似；两者总是同时返回 |
| 差值区间 | `percentile_interval` / `bca_bootstrap` | 百分位口径与 v1.1 完全一致（seed=1、2000 次、索引 49/1949）；BCa 为校偏补充 |
| 样本充分性 | `mde_proportions` / `required_pairs` / `power_at` | 正态近似，`discordance` 缺省取独立上界 `2p(1-p)` |
| 组合视图 | `analyze_paired` | 上述全部量 + `notes` / `underpowered` / `sample_insufficient` |

**第三态“证据不足”**：`evaluate_gate` 保持二值、逐字不变（历史回放与既有凭证不受影响），
`assess_gate` 在它之上判定 `pass` / `fail` / `insufficient`。只有“统计判据未过 +
题数 < `min_sample` + 观测增益 > 0”才是 `insufficient`；成本比、F1 退化、逐题回归率
属于结构性硬否决，小样本不能洗白。`insufficient` 的候选不推进版本，凭证记为
`insufficient`。

**选择偏差**：每轮比较都记账，报告 `comparisons`、`effective_comparisons`、
`candidate_counts`。**不同候选数 > 3** 时输出族错误率警告与 Bonferroni 建议 α：

```
已比较 5 个候选（>3 个，有效比较 5 次），族错误率未校正：
单次 α=0.05 下至少一次假阳性的上界约 0.25；建议按 Bonferroni 使用 α=0.0100，或改用独立 holdout 复验
```

```bash
python -m harness.cli stats  --db <状态目录>    # 重算 Wilson / McNemar / BCa / MDE 与选择偏差
python -m harness.cli trace  --db <状态目录>    # JSONL 事件摘要（--raw 逐行、--event 过滤）
python examples/bench_write.py                  # SQLite WAL 写入压测（1000 条输出）
```

## 证据边界（请先读）

- **Prompt 类演化已实现**（mock 预置补丁 + LLM 反思提议器），但只用本地 mock 端点验证过闭环；**Memory / Tool / Skill 三类仍只有契约层，没有策略实现**。
- **执行器已分层**：`OfflineExecutor` 仍不含任何发送能力，`OnlineExecutor` 需要显式传入 runner 才会调用模型；`GateSpec.require_offline=True` 时门禁会拒绝真实运行。真实 API 实验（R1/R2/R3）由仓库外的研究脚本完成，逐题账本未随包分发。
- **校园问答示例是确定性演示**：10 份合成制度、36 道合成题，11/12 → 12/12 由预置规则解释器产生，`api_calls=0`；**没有真实高校授权、没有学生试用、没有公网部署**。
- **没有公开用户**，也没有已合并的上游 PR；上游适配补丁属于计划，不冒充已提交。
- R1/R2 各 30 题、单种子、单次划分；R2 的成本比为**每题平均延迟代理**，不是账单费用。
- 在线闭环的“效果”没有真实模型证据：演示里 2/6→5/6 是 MockRunner 的预置规则，不是模型能力。
- “一键复现全部实验”不成立：开源包保证复现的是**测试、离线演示、门禁回放与 mock 闭环**，不是重新花钱跑模型。

完整清单见 [docs/limitations.md](docs/limitations.md)。

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/quickstart.md](docs/quickstart.md) | 安装顺序、命令、故障排查 |
| [docs/online-evolution.md](docs/online-evolution.md) | 接端点、成本控制、并发、断点续跑 |
| [docs/statistics.md](docs/statistics.md) | 统计口径：Wilson、McNemar、BCa、MDE、证据不足与选择偏差 |
| [docs/protocol.md](docs/protocol.md) | 协议冻结、导入导出、`protocol_hash`、测试集只跑一次 |
| [docs/architecture.md](docs/architecture.md) | 模块、状态机、SQLite 结构、不变量 |
| [docs/gate-spec.md](docs/gate-spec.md) | 两级门禁阈值、理由、历史回放结果 |
| [docs/decision-receipt.md](docs/decision-receipt.md) | 凭证字段、哈希规则、链校验与威胁模型 |
| [docs/evidence.md](docs/evidence.md) | 每个证据文件的口径与复算方式 |
| [docs/limitations.md](docs/limitations.md) | 未实现、未验证、不可宣称的内容 |
| [docs/faq.md](docs/faq.md) | 常见质疑与回答 |
| [docs/roadmap.md](docs/roadmap.md) | 下一阶段计划 |

## 示例

- `examples/gate_replay.py`：用 R1/R2 的配对计数复算两级门禁决策，输出 `gate_replay.json`；
- `examples/evolve_demo.py`：两个场景——场景 A 跑完整在线闭环（3 轮、1 拒 1 收、凭证链校验）；场景 B 连续比较 5 个候选，触发族错误率警告并报告有效比较次数；
- `examples/bench_write.py`：SQLite WAL 写入压测（1000 条输出；单写、并发写、写时读三组吞吐与分位延迟）；
- `examples/campus_qa/`：校园缓考问答的第二个场景（FastAPI + 3 页面 + 34 项测试），复用同一套 harness。

## 许可

本项目自研代码采用 **MIT** 许可，版权人写作 “2026 循证演化 Team”。

第三方组件（GEPA、gepa-artifact、DSPy、bm25s、PyStemmer、HotpotQA 等）按各自许可保留，详见 [THIRD_PARTY.md](THIRD_PARTY.md) 与 `licenses/`、`upstream/` 下的原始许可文件。HotpotQA 原文与逐题账本**不再分发**，仓库只保留 ID、计数与聚合指标。
