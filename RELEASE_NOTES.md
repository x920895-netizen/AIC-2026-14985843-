# RELEASE NOTES

## v1.2.0（2026-10-29）

目标：把“门禁通过了”变成**可复核的统计结论**——说清楚样本够不够、比了几个候选、
这次决策绑定的是哪份协议。v1.1.0 的 104 项测试全部保留并通过，总数 **104 → 234**；
`evaluate_gate` 的二值接口逐字未变，历史回放与既有凭证不受影响。

1. **统计口径**（`harness/stats.py`）：Wilson 区间（不用 Wald）、精确 McNemar
   （含连续性校正，两种 p 值同时返回）、百分位/BCa bootstrap（百分位口径与 v1.1
   完全一致：seed=1、2000 次、索引 49/1949）、MDE / required_pairs / power。
   用已知分布锁定：Wilson 参考值、二项/卡方参考 p、BCa 与正态理论区间（±1%）。
2. **第三态“证据不足”**：`assess_gate` 输出 `pass` / `fail` / `insufficient`。
   只有“统计判据未过 + n < min_sample + 观测增益 > 0”才是 insufficient；成本、F1、
   回归率是结构性硬否决，小样本洗不白。`evidence` 里带 MDE、`below_mde`、注记。
3. **选择偏差**：记录比较次数、**有效比较次数**、每个候选被比较次数；
   不同候选数 > 3 时输出族错误率警告与 Bonferroni 建议 α（示例：5 个候选 → α=0.0100）。
4. **幂等与恢复**：重复 `accept` 返回既有凭证（`idempotent=True`），不再抛“状态已变”；
   `evolve` 在轮次内按 propose/run/validate/decide 分阶段持久化，5 个中断点
   （before/after register、after run、after validate、after accept）都能续跑。
5. **协议**（`harness/protocol.py`）：`ProtocolSpec` 冻结划分/评价器/阈值/重复/种子/
   `min_sample`/测试集策略，导入导出稳定哈希；凭证记录 `protocol_hash`；
   `test_policy=once` 由 `TestSetGuard` 硬执行（自有运行不计入，续跑不被自己挡住）。
6. **可观测**（`harness/trace.py`）：JSONL 事件日志，固定六字段；CLI 新增 `trace` 与
   `stats`，以及 `protocol-export` / `protocol-import` / `protocol-show`。
7. **并发与限流**：令牌桶（可注入时钟）+ `Retry-After`（秒数与 HTTP-date，带上限）；
   SQLite 统一 WAL；`examples/bench_write.py` 压 1000 条输出。
8. **修复**：runner 重试时把响应头当请求头（丢 Authorization、带错 Content-Length，
   导致 429/500 重试直接断连）；选择偏差警告误把版本数当候选数；`SelectionTracker`
   写入的 p 值未被读取。

验证记录（本机 Windows 10.0.26200 / Python 3.12，全部离线）：

```text
py -3 -m pytest tests/ -q
234 passed, 51 subtests passed in 234.77s

py -3 examples/evolve_demo.py --json
场景 A：3 轮 REJECT/ACCEPT/STOP；测试集 50.00% → 83.33%（+33.33pp）；凭证 2 条，链 verified=True
场景 B：5 轮 REJECT/ACCEPT/REJECT/ACCEPT/REJECT；测试集 12.50% → 75.00%（+62.50pp）
        有效比较 5、不同候选 5、每轮比较次数 [1,1,1,1,1]；凭证 5 条，链 verified=True
        族错误率警告：建议按 Bonferroni 使用 α=0.0100，或改用独立 holdout 复验
两个场景全部断言通过；耗时 9.38 秒

py -3 examples/gate_replay.py
R1 screen PASS / release FAIL(成本比 1.6477)；R2 screen FAIL(EM) / release FAIL(4 条理由)
—— 与 v1.0.0 完全一致，决策未变

py -3 scripts/demo_submission.py --output <dir>
6 事件：初始化 / 独立验证通过 / 原子接受 / 退化候选被拒绝 / 重新打开持久状态 / 显式回滚
api_calls=0

py -3 examples/bench_write.py
journal_mode=WAL, synchronous=FULL, 1000 条
单写者逐条追加 22.937s → 44 条/s（p50 22.2ms / p95 32.3ms）
并发写 8 线程   6.108s → 164 条/s（p50 10.1ms / p95 117.3ms，0 错误）
写时读 200 次   3.601s（p50 14.2ms / p95 37.5ms，0 错误）
```

已知边界：

- 统计口径是**流程约束**，不是效果证据；MDE 给出的是“这个样本最多能说明什么”，
  不能把“未检出”写成“无效应”。
- 精确 McNemar 不做多重比较校正；族错误率只在选择偏差摘要里提示，门禁不会自动
  改阈值（那会改变历史决策）。
- 令牌桶与 `Retry-After` 只用本地 mock 端点验证，没有对真实服务商做限流压测。
- 压测数字是本机结果（WAL + `synchronous=FULL`），不代表其他磁盘或操作系统。

## v1.1.0（2026-10-29）

目标：把“离线门禁原型”升级成**可接真实模型的在线自进化闭环**，v1.0.0 的 62 项测试零回归。

1. **执行器分层**：`Executor` 协议 + `OfflineExecutor`（仍是零发送能力）+ `OnlineExecutor`（`ThreadPoolExecutor` 并发、每任务预留/结算、增量落库、断点续跑）。验收：8 路并发、失败任务只重跑一次。
2. **OpenAI 兼容 runner**：任意 `base_url`、环境变量取 key、429/5xx/超时指数退避（≤4 次）、`response_format=json_object`、usage 计费、无 usage 按预留上界记账并标记、延迟与原始响应可关。用本地 `http.server` 覆盖 429/500/超时/缺 usage。
3. **反思提议器**：最多 8 条验证集失败轨迹 + 上一轮否决理由 → 严格 JSON（diagnosis/change_summary/expected_gain/risk/new_prompt）；空、超长、未变更一律拒绝，重试一次后返回 `None`，不会硬塞候选。
4. **`evolve()` 闭环**：基线 → 逐轮 propose/register/run/validate/accept|reject（写凭证）→ 测试集只跑一次且不参与选择。停止条件：轮次、连续 2 次拒绝、预算耗尽、提议器放弃。成本只花在验证集选择上。
5. **可续跑、可审计**：运行级 `resume_run_id` 跳过已完成 `(task_id, repeat)`；演化级 `resume(run_id, proposer, runner)` 从已提交轮次继续；每轮写 `evolution_runs` 与 `evolution_round` 事件。
6. **门禁模式可配**：`GateSpec.require_offline`（默认 False）。离线演示/单测设 True（原先的“非 simulation 即拒”），在线闭环设 False，并额外校验真实调用的 `api_calls`/费用与结算槽位一致。
7. **CLI 与报告**：`init` / `run` / `evolve` / `report`（均 `--help` 可用）+ 原有凭证命令；`harness/report.py` 输出版本链、候选与门禁理由、轮次表、最终 val/test 指标、凭证链校验与成本分账。
8. **测试与演示**：62 → **104 项**（新增在线 runner 11、反思提议器 11、闭环 8、报告 4、CLI 6、评分等价 2）。`examples/evolve_demo.py`：12 道合成题、3 轮（第 1 轮 REJECT、第 2 轮 ACCEPT、第 3 轮停止）、2 条凭证、链校验通过、2–3 秒完成、`external_api=false`。

验证记录：

```text
py -3 -m pytest tests/ -q
104 passed, 40 subtests passed

py -3 examples/evolve_demo.py
3 轮：REJECT / ACCEPT / STOP；凭证 2 条；链校验 verified=True；耗时约 2.6 秒

py -3 examples/gate_replay.py
R1 screen PASS / release FAIL(成本比)；R2 两档 FAIL（release 4 条理由），与 v1.0.0 一致

py -3 scripts/demo_submission.py --output <dir>
6 事件通过，api_calls=0
```

边界：在线闭环只用本地 mock 端点验证，**没有真实模型的大样本结果**；Memory/Tool/Skill 策略仍未实现；演化级续跑只能恢复到轮次边界。历史 R1/R2 仍来自外部研究脚本，不与 mock 结果混为一谈。

## v1.0.0（2026-10-29）

发布日期：2026-10-29　许可：MIT（版权人 “2026 循证演化 Team”）

## 八条要点

1. **两级门禁成为默认能力**：`harness/gates.py` 提供 `screen`（探索筛查）与 `release`（发布门禁）两档预设和纯函数 `evaluate_gate(metrics, spec)`。`release` 阈值与历史默认值逐项相同，因此 `GateSpec(...)` 行为不变（向后兼容）；`CandidateValidator` 复用同一判据，否决理由措辞保持不变。

2. **每次 accept / reject 都有决策凭证**：`harness/receipt.py` 生成 13 字段的 SHA-256 凭证，按 `prev_receipt_hash` 链式写入 SQLite `receipts` 表；`verify_chain(conn)` 与 `harness.cli verify-receipts` 可检测内容篡改、伪造自洽凭证与断链。凭证与发布在同一事务内提交。

3. **历史门禁可复算**：`examples/gate_replay.py` 仅用 R1/R2 的配对计数与指标，按 harness 同款配对 bootstrap（seed=1，2000 次，索引 49/1949）重算区间。结果：R1 `screen` 通过、`release` 仅因“候选推理成本比超限”（1.6477 > 1.2）被拦；R2 两档皆否，`release` 命中 4 条理由。

4. **真实结果口径唯一**：README 只保留 R1 独立测试 30 题 EM 63.33% → 76.67%（+13.33pp，回归 0/23）与 R2 新独立测试 30 题 76.67% → 70.00%（−6.67pp，回归 2/23）；明确写出 R1 McNemar p=0.125、不宣称统计显著，R2 作为负面结果保留。

5. **清除全部合成与误导内容**：早期合成预测目录、合成汇总证据文件，以及引用其题量/分数/“多次运行稳定性”情景的表述已从本包删除；校园应用 `/api/research` 中对应的合成字段与页面提示同步移除，`runs/round3g`、`round3h` 的 `statistics.json` 替换为聚合摘要。

6. **可复现性修复**：`pytest.ini` 限定 `testpaths=tests`（解决根目录收集 upstream 导致的错误）；安装顺序写进 README 与 quickstart（先 `pip install ./upstream/gepa`，否则 PyPI 同版本 `gepa 0.1.4` 缺 `best_score`，表现为 32 passed / 1 failed）；三个依赖原始账本的研究脚本在缺文件时给出可读提示并以退出码 2 结束；`demo_submission.py` 缺 dspy 时给出可读指引。

7. **测试从 33 项扩到 62 项并全绿**：新增 `test_gates.py`（11）、`test_receipts.py`（8）、`test_gate_replay.py`（10）。`py -3 -m pytest tests/ -q` → `62 passed, 19 subtests passed in 48.08s`。离线演示 6 个事件全部通过并产生 2 条可校验凭证；校园示例 34 项测试通过（独立套件，不累计）。

8. **交付形态完整**：MIT LICENSE、CITATION.cff、README、8 篇 docs、CONTRIBUTING、CODE_OF_CONDUCT、SECURITY、CHANGELOG、pyproject（`evidence-evolve` 1.0.0，`requires-python >=3.10`，`research` / `campus` / `dev` 可选依赖）、CLI 入口、GitHub CI（Python 3.12）、Issue/PR 模板、`SHA256SUMS.txt`、12 个依赖许可与 `upstream/` 下的 GEPA 原件。

## 验证记录

```text
py -3 -m pytest tests/ -q
62 passed, 19 subtests passed in 48.08s

py -3 scripts/demo_submission.py --output <dir>
6 events passed, api_calls=0, actual_api_cost_cny=0

py -3 -m harness.cli verify-receipts --state-dir <dir>/state
{"verified": true, "count": 2, "head": "..."}

py -3 examples/gate_replay.py --output evidence/gate_replay.json
R1 screen PASS / R1 release FAIL(候选推理成本比超限)
R2 screen FAIL(主指标EM增益不足) / R2 release FAIL(4 条理由)
```

## 边界（不随版本变化）

四类演化策略未实现；执行器仅离线；校园应用为确定性演示、无真实授权与用户；没有已提交的上游 PR；R1/R2 为各 30 题、单种子，不能支撑普适结论。详见 `docs/limitations.md`。
