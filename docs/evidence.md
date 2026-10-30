# 证据说明

所有对外数字只有一个口径：本仓库 `evidence/` 与 `runs/` 下的脱敏汇总。原始逐题、逐请求账本**不随包分发**（含 HotpotQA 语料片段），因此“一键重跑全部实验”不成立；可一键复现的是测试、离线演示与门禁回放。

## 文件清单

| 文件 | 内容 | 可否复算 |
| --- | --- | --- |
| `evidence/demo.json` | 离线演示的 6 个事件、指标与理由 | 是：`scripts/demo_submission.py` |
| `evidence/acceptance-01.json` | 33 项测试的逐项记录（28.8 秒，历史快照） | 是：`pytest tests/`（当前 234 项） |
| `evidence/acceptance-02.json` | 24 项底座复测记录（不累加） | 是 |
| `evidence/historical_r1.json` | R1 配对计数、指标、成本、bootstrap 与 McNemar | 是：`examples/gate_replay.py` 复算区间与决策 |
| `evidence/historical_r2.json` | R2 同上（成本为延迟代理） | 是 |
| `evidence/gate_replay.json` | 两级门禁回放输出 | 是：运行脚本生成 |
| `evidence/campus_browser_record.json` | 校园应用本机 HTTP 调用记录、版本号、门禁结论 | 是：`examples/campus_qa` + 34 项测试 |
| `evidence/test_summary.json` | 当前测试数量与耗时 | 是：`pytest tests/` |

## R1（真实 API）

- 数据：HotpotQA-Mini，30/30/30，种子 1，任务模型 `qwen3.8-flash`，反思模型 `qwen3.8-max-0902`，非思考模式。
- 独立测试 30 题：19/30 → 23/30 正确，修复 4、回归 0；EM 63.33% → 76.67%；F1 70.23% → 82.15%。
- 配对 bootstrap 95% 区间 [+3.33pp, +26.67pp]；精确 McNemar p=0.125（4 个不一致对，双侧）。**不宣称统计显著。**
- 成本：测试阶段 ¥0.1591197 → ¥0.2621799（1.647690×）；整轮 ¥9.98503，2,526 次 API，462 次 metric calls，9 个候选，842.4 秒。

## R2（真实 API，负面结果）

- 数据：同池新抽 30 题（排除原 90 题），R1 候选 → 人工改进候选。
- 23/30 → 21/30，新增答对 0、回归 2；EM 76.67% → 70.00%；F1 82.00% → 80.90%。
- 回归率 2/23 = 8.70%；bootstrap 区间 [−16.67pp, 0.00pp]；McNemar p=0.5000。
- 成本：整轮 ¥2.18887，1,096 次 API，272 次评价，8 次失败请求。
- 成本比 1.130573 是**每题平均延迟代理**（21.2449s → 24.0189s），不是账单金额。

## R3 诊断（真实 API，210 次生成）

| 子实验 | 调用 | 观察 |
| --- | ---: | --- |
| 3-A 指令稳定性 | 30 | 两道回归题两组均 0/3 |
| 3-B 原文消融 | 30 | 排名题关闭原文 3/3、开启 0/3；人名题未改善 |
| 3-C 输入审计 | 0 | 两例请求可逐字重建 |
| 3-D 输入重放 | 60 | A/B/C/D 各 15 次，正确 0/1/3/1 |
| 3-E seed 核验 | 0 | 接口接受 seed 不足以确认后端有效 seed |
| 3-F 摘要对照 | 20 | 排名题各 2/5；人名题各 0/5 |
| 3-G 条款消融 | 30 | 排名题原指令 3/10，删第 4 条+示例 6/10；人名题两组 0/5 |
| 3-H 正文/示例析因 | 40 | A/B/C/D 各 10 次，正确 7/4/2/2；world 形式 3/5/7/7 |

合计真实 API 请求 2,526 + 1,096 + 210 = **3,832 次**，估算费用 **¥12.9063**。

`runs/round3g/statistics.json` 与 `runs/round3h/statistics.json` 已替换为**聚合摘要**（题序、正确计数、诊断类别、耗时与成本），不再包含问题文本、检索正文、参考文本与原始回答。

## 工程底座

- 原 33 项测试通过 / 0 失败 / 7 subtests / 约 40.5 秒（正确安装 `./upstream/gepa` + dspy 3.4.1）。
- 当前 234 项测试：104 项（v1.1 累计）+ 130 项 v1.2 新增（统计口径 36、选择偏差 15、
  协议 20、事件日志 11、限流 20、证据不足 13、幂等与恢复 11、写入压测 3、CLI 扩展 1）。
- v1.2 关键口径由已知分布锁定：Wilson 参考值、精确 McNemar 二项/卡方参考 p、
  BCa 与正态理论区间（±1% 相对误差）；5 个轮次内中断点分别续跑并比对最终决策。
- 离线演示 6 个事件：初始化 → 独立验证通过（current 未切换）→ 原子接受 → 退化候选拒绝（4 条理由）→ 重开恢复（restored=true, candidates=2）→ 回滚祖先。
- 演示参数：4 道合成任务、预置输出、`api_calls=0`、真实费用 0；仅验证控制流，不代表模型能力。

## 校园智服（第二场景）

- 10 份合成制度，36 道题（12 提议 / 12 验证 / 12 留出），FastAPI + 3 页面。
- 34 项应用测试通过 / 39 subtests / 约 53 秒。
- `/api/chat` 返回带 CP03/CP04/CP05 条款引用的结构化答案。
- `/api/evolution/run-demo`：11/12 → 12/12，修复 1、回归 0、`api_calls=0`；重复调用 409。
- 真实外部 API 调用 0 次；无真实高校制度授权、无学生试用、无公网部署。**不得写成真实校园上线。**

## 在线闭环（v1.1）

| 证据 | 内容 | 复现方式 |
| --- | --- | --- |
| `examples/evolve_demo.py` 输出 | 12 道合成题（6 验证 / 6 测试）；3 轮：bad_patch 拒绝 → good_patch 接受 → 不再提议；测试集 50%→83.33%；2 条凭证；模拟费用约 ¥0.006；`external_api=false` | `python examples/evolve_demo.py` |
| `tests/test_online_runner.py` | 本地 `http.server` 假端点：成功与鉴权、429 重试、500 重试、超时、usage 计费、usage 缺失、JSON 模式、无 Key、8 路并发、断点续跑 | `pytest tests/test_online_runner.py` |
| `tests/test_evolve_loop.py` | 1 拒 1 收、测试集不参与选择、凭证链完整、中断后续跑、连续 2 拒停止、预算耗尽 | `pytest tests/test_evolve_loop.py` |

**这些数字是 mock 行为，不是模型能力**：MockRunner 按 prompt 规则返回，`api_calls` 计数但 `external_api=false`、真实费用 0。仓库不包含任何“真实模型在线闭环增益”的结论。

## 如何自己重算门禁

```bash
python examples/gate_replay.py --output evidence/gate_replay.json
```

脚本会先校验 `paired` 计数与存档 EM 是否自洽、bootstrap 区间是否与存档一致，任一不符即失败退出（fail closed），然后再判定。`tests/test_gate_replay.py` 锁死了全部数字与决策。
