# 变更日志

格式参考 Keep a Changelog，版本号遵循语义化版本。所有日期为 UTC。

## [1.2.0] - 2026-10-29

把“门禁通过了”升级成“门禁为什么通过、样本够不够、比了几个候选”。v1.1 的 104 项测试
全部保留并通过，测试总数 104 → **234**。门禁的二元判定 `evaluate_gate` 逐字未变，
历史回放与既有凭证不受影响。

### 新增

- **统计口径 `harness/stats.py`**：`wilson_interval`（Wilson score，小样本不产生负端点）、
  `mcnemar_exact`（默认精确二项；`continuity=True` 用 `(|b-c|-1)²/(b+c)` 卡方近似；两种
  p 值始终同时返回）、`percentile_interval`（与 v1.1 门禁完全同口径：seed=1、2000 次、
  索引 49/1949）、`bca_bootstrap`（偏差校正 + jackknife 加速，并列值按半分计避免 z0 发散）、
  `mde_proportions` / `required_pairs` / `power_at`、`analyze_paired`（组合视图 + 方法学注记）。
- **第三态“证据不足”**：`gates.assess_gate` 返回 `pass` / `fail` / `insufficient`。
  `insufficient` 仅当“统计判据未过 + 题数 < `min_sample` + 观测增益 > 0”；成本比、F1 退化、
  逐题回归率是结构性硬否决，小样本不能洗白。`evidence` 字段给出 `mde`、`below_mde`、
  `underpowered`、`hard_vetoes`、`statistical_vetoes` 与中文注记。
- **选择偏差 `harness/selection.py`**：`build_summary` / `SelectionTracker` 记录
  `comparisons`、`effective_comparisons`、`candidate_counts`、`version_counts`；
  **不同候选数 > 3** 输出族错误率警告与 Bonferroni 建议 α。候选登记时累加
  `comparison_count`，每轮记录都带这个数。
- **协议 `harness/protocol.py`**：`ProtocolSpec`（划分/评价器/阈值/重复/种子/`min_sample`/
  `test_policy` 冻结），`to_dict`/`from_dict`/`to_json`/`from_json`/`save`/`load`，
  `protocol_hash`（`created_at` 不参与，跨导出稳定），`protocol_warnings`，
  `TestSetGuard` 实现 `test_policy="once"` 的测试集只跑一次硬约束（自有运行不计入）。
  凭证新增固定字段 `protocol_hash`。
- **可观测 `harness/trace.py`**：JSONL 事件日志（`trace/1`），固定
  `event/ts/run_id/candidate_id/duration_ms/cost` 六字段；线程安全、写失败计数、坏行跳过。
  `EvolutionFoundation` 默认写到 `<状态目录>/trace/events.jsonl`。
- **限流 `harness/ratelimit.py`**：线程安全 `TokenBucket`（可注入 clock/sleep，便于测试）、
  `RateLimitTimeout`、`parse_retry_after`（秒数与 HTTP-date，支持上限）。
- **压测 `examples/bench_write.py`**：1000 条输出，三组测量（单写者逐条追加、8 线程并发写、
  写时读），输出吞吐与 p50/p95。
- **CLI**：`stats`（重算 Wilson/McNemar/BCa/MDE + 选择偏差）、`trace`（`--raw`/`--event`/
  `--limit`）、`protocol-export` / `protocol-import` / `protocol-show`。
- **测试**：新增 `test_stats.py`(36)、`test_selection.py`(15)、`test_protocol.py`(20)、
  `test_trace.py`(11)、`test_ratelimit.py`(20)、`test_evidence.py`(13)、
  `test_resume_idempotency.py`(11 + 5 个中断点子测试)、`test_bench_write.py`(3)，
  `test_cli.py` 增至 7 项；总数 104 → **234**。

### 变更

- 门禁评价报告（`evaluations`）新增 `verdict`（pass/fail/insufficient）与 `evidence`；
  凭证 `decision` 增加 `insufficient`（候选不推进 current 指针）。
- `accept` 幂等：候选已是 `accepted` 时返回既有版本与既有凭证（`idempotent=True`），
  不再抛“状态已变”；若已接受但凭证缺失，仍按 `StateConflict` 拒绝。
- `evolve` 改为**轮次内分阶段持久化**（propose / run / validate / decide 四个
  `stage.step` + 5 个持久化边界），任一步中断都可 `resume(run_id, proposer, runner)`
  续跑；测试阶段同样可续跑并复用既有 `base_run_id` / `final_run_id`。
- `OnlineExecutor` 支持 `rate_limit`（`TokenBucket` 或每秒请求数），与 runner 级限流可叠加；
  预算账本同一 `scope_id` 只结算一次。
- OpenAI 兼容 runner 尊重 429/503 的 `Retry-After`（含 HTTP-date、可设上限），其余错误
  仍走指数退避；新增 `rate_limiter` 参数。
- SQLite 统一启用 **WAL**（启动时校验，失败即报错）并保留 `synchronous=FULL` 与
  30s `busy_timeout`。
- `GateSpec` 新增 `min_sample: int = 30`；`ProtocolSpec.gate_spec()` 把它一并冻结。
- `harness.__version__ = "1.2.0"`；`pyproject.toml` / `CITATION.cff` 同步。

### 修复

- **runner 重试串头**：`complete()` 里 `status, body, headers = self._post(...)` 复用变量名，
  重试时把**响应头当请求头**发回——丢掉 `Authorization` 并带上上一次响应的
  `Content-Length`，导致 429/500 重试在本地端点直接 `RemoteDisconnected`。已用独立变量接收
  响应头并加回归测试（重试期间 `Authorization` / `Content-Type` 保持不变）。
- **选择偏差警告口径**：v1.2 初版把“版本数”也计入触发条件，3 个候选（4 个版本）就会误报；
  现在只由**不同候选数**触发，版本数仅作参考字段。
- **`SelectionTracker` 的 p 值未被读取**：`build_summary` 只认 `statistics.mcnemar_p`，
  记录器写入的 `p_value` 被忽略，导致 `min_p_value` 恒为 `None`；现在两种形状都支持。

### 文档

- 新增 `docs/statistics.md`（公式、校验值、三态规则、选择偏差口径、边界）与
  `docs/protocol.md`（字段、哈希、导入导出、凭证绑定、测试集只跑一次）。
- README 增加“统计口径（v1.2）”章节，更新架构树、示例、文档索引与快速开始；
  `docs/architecture.md`、`docs/quickstart.md`、`docs/roadmap.md` 同步。

### 边界

- 统计口径是**流程约束**，不是效果证据：MDE 为小样本提供了“能说明什么”的上限，
  但不能把“未检出”写成“无效应”。
- 精确 McNemar 不做多重比较校正；族错误率只在选择偏差摘要里提示，门禁不会自动
  切换阈值（那会改变历史决策）。
- 令牌桶与 `Retry-After` 只用本地 mock 端点验证；没有对真实服务商做过限流压测。
- 压测数字是本机 Windows 结果（WAL + `synchronous=FULL`），不代表其他磁盘/OS。

## [1.1.0] - 2026-10-29

把“离线门禁原型”升级成可接真实模型的在线自进化闭环；v1.0.0 的 62 项测试全部保留并通过。

### 新增

- **执行器分层**：`harness/execution.py` 定义 `Executor` 协议；`OfflineExecutor` 保持不变；新增 `OnlineExecutor`（`ThreadPoolExecutor` 并发、每任务预留/结算、按完成情况增量落库、断点续跑、错误即停）。
- **OpenAI 兼容 runner**：`harness/runners/openai_compatible.py`，任意 `base_url`、环境变量取 key、429/5xx/超时指数退避（最多 4 次）、`response_format=json_object`、usage 计费、无 usage 按预留上界记账并标 `usage_reported=false`、延迟与原始响应（可关）。
- **Mock runner**：`harness/runners/mock.py`，按 task_id + prompt 规则确定性返回，`api_calls` 真实计数、`external_api=false`。
- **提议器**：`harness/proposers/reflective.py`（最多 8 条失败轨迹 + 上一轮否决理由 → 严格 JSON；校验非空/长度/必须不同，失败重试一次后返回 `None`）与 `harness/proposers/mock.py`（good/bad 预置策略、按轮次脚本）。
- **在线闭环**：`EvolutionFoundation.evolve()`（基线 → 逐轮提议/验证/判定/凭证 → 测试集汇报）；`resume(run_id, proposer, runner)` 从已提交轮次续跑；新增 `evolution_runs` 集合与 `evolution_round` 事件。
- **报告**：`harness/report.py` 从 SQLite 生成 Markdown（版本链、候选与门禁理由、演化轮次、最终 val/test 指标、凭证链校验、成本）。
- **CLI**：`init` / `run` / `evolve` / `report`，保留 `inspect` / `receipts` / `verify-receipts` / `rollback`，全部 `--help` 可用。
- **演示**：`examples/evolve_demo.py`，12 道合成题（6/6），3 轮（拒绝→接受→停止）、2 条凭证、模拟费用，2–3 秒完成。
- **测试**：新增 `test_online_runner.py`（本地 `http.server`：成功、429、500、超时、usage 缺失、JSON 模式、无 Key、8 路并发、续跑）、`test_reflective_proposer.py`、`test_evolve_loop.py`、`test_report.py`、`test_cli.py`、`test_scorer_equivalence.py`；总数 62 → **104**。

### 变更

- `GateSpec` 增加 `require_offline: bool = False`：原来“一律拒绝非离线运行”改为按字段控制；离线演示/单测显式设 True，在线闭环设 False。
- `CandidateValidator` 拆出 `summarize()` / `compare()`，门禁与测试集汇报共用同一套重算逻辑；真实运行额外校验 `api_calls` 与真实费用和结算槽位一致。
- `BudgetLedger` 增加 `simulated_cny` / `real_cny` 分账、`mode` 参数、`remaining()` / `stop()`；`api_calls` 对 mock 也计数。
- `EM` 改为本地 HotpotQA 规范化实现（`scripts.scoring.normalize`），去掉演示/门禁对 DSPy 的导入依赖；与 `dspy.evaluate.answer_exact_match` 的等价性由 `test_scorer_equivalence.py` 逐例锁定。
- `EvolutionFoundation.run()` 支持 `executor` 与 `resume_run_id`，并按 `GateSpec.task_ids` 过滤/排序数据集任务（验证集可以是数据集的子集）。
- `EvolutionFoundation.resume()` 支持可选 `run_id`；无参时行为与 v1.0.0 完全一致。
- SQLite 连接超时 10s → 30s；`pyproject.toml` 版本 1.1.0，新增 `online = ["openai>=2.0"]` 可选依赖。
- 文档：新增 `docs/online-evolution.md`，更新 `architecture.md`、`quickstart.md`、`limitations.md`、`faq.md`、`roadmap.md`、`evidence.md`、README。

### 边界

- 在线闭环只用**本地 mock 端点**验证；没有在评审环境用真实模型跑新的大样本，不宣称真实增益。
- Memory / Tool / Skill 三类策略仍未实现；演化级续跑只能恢复到轮次边界。

## [1.0.0] - 2026-10-29

首个公开成果包快照，面向 2026 AIC · AI+开源赛道提交。八条要点：

1. **两级门禁落地**：新增 `harness/gates.py`，提供 `screen`（探索筛查）与 `release`（发布门禁）两档预设与纯函数 `evaluate_gate`；`release` 与历史默认值完全一致，`CandidateValidator` 复用同一判据，否决理由措辞不变。
2. **决策凭证链**：新增 `harness/receipt.py`，每次 accept / reject 写入 13 字段的 SHA-256 凭证，按链式存放于 SQLite `receipts` 表；提供 `verify_chain(conn)` 检测篡改、伪造与断链，并集成到 `controller.accept` / `validate` / 新增的 `controller.reject`。
3. **历史门禁回放**：新增 `examples/gate_replay.py`，只用 R1/R2 的配对计数复算配对 bootstrap 区间并输出两档决策（R1 探索通过、发布仅因成本比被拦；R2 两档否决，发布命中 4 条理由）。
4. **可复现性修复**：`pytest.ini` 限定 `testpaths=tests`；给出必须先 `pip install ./upstream/gepa` 的安装顺序，避免 PyPI 版 `gepa 0.1.4` 缺 `best_score` 导致的 32 passed / 1 failed；`scripts/report_round1.py`、`report_round2.py`、`analyze_round2.py` 在缺原始账本时输出可读提示并以退出码 2 结束；`scripts/demo_submission.py` 在缺 dspy 时给出可读报错。
5. **清除合成与误导内容**：删除早期合成预测目录、`evidence/` 下的合成汇总文件，以及 README、文档、校园接口中的相关表述（含题量、分数与“多次运行稳定性”情景）；README 只保留 R1 +13.33pp / R2 −6.67pp 的真实结果，并写明 McNemar p=0.125 不宣称显著。
6. **证据脱敏**：`evidence/` 只保留演示、验收、R1/R2 脱敏汇总、门禁回放、校园浏览器记录与测试摘要；`runs/round3g`、`round3h` 的 `statistics.json` 替换为聚合摘要；配置文件中的内部工作空间地址替换为公开兼容地址。
7. **文档与治理文件**：补齐 README（一句话定位、真实结果表、30 秒快速开始、架构、证据边界、许可）与 8 篇 `docs/`；新增 MIT `LICENSE`（版权人 “2026 循证演化 Team”）、`CITATION.cff`、`CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`、`SECURITY.md`、Issue/PR 模板、CI（Python 3.12）。
8. **测试扩充**：33 项原有测试 + 29 项新增（门禁 11、凭证 7、回放 11）= **62 项全绿**；离线演示 6 个事件全部通过并产生 2 条可校验凭证；校园示例 34 项测试通过。

### 新增

- `harness/gates.py`、`harness/receipt.py`、`harness/cli.py`
- `examples/gate_replay.py`、`examples/campus_qa/`
- `tests/test_gates.py`、`tests/test_receipts.py`、`tests/test_gate_replay.py`
- `evidence/historical_r1.json`、`historical_r2.json`、`gate_replay.json`、`campus_browser_record.json`、`test_summary.json`
- `docs/quickstart.md`、`architecture.md`、`gate-spec.md`、`decision-receipt.md`、`evidence.md`、`limitations.md`、`faq.md`、`roadmap.md`

### 变更

- `GateSpec` 新增 `screen()` / `release()` 类方法；评价协议白名单 `EVALUATOR_IDS`（新增 `campus-policy-v1`）。
- `FoundationStore.initialize()` 支持 `evaluator_id` 参数。
- `EvolutionFoundation.resume()` 返回值新增 `receipts`。
- `Controller.validate()` 的决策 `verdict` / `mode` 可来自评价报告（用于校园场景）。
- `pyproject.toml`：包名 `evidence-evolve`，版本 1.0.0，`research` / `campus` / `dev` 可选依赖。

### 移除

- 早期的合成预测目录与预览目录（均未进入 v1.0.0）
- `evidence/` 下的合成预测汇总文件
- 各文档中引用合成预测分数与“多次运行稳定性”情景的表述
- 校园应用研究页中的合成预测字段与页面提示
