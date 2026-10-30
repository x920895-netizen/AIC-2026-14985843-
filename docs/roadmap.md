# Roadmap

原则：先补证据，再扩能力；不把“计划”写成“已完成”。

## v1.1（近期，无需新预算）

- [ ] 公开仓库与 Issue 模板启用，补齐 `good first issue` 标签
- [ ] CI 增加 Windows / macOS 矩阵（当前仅 Ubuntu + Python 3.12）
- [ ] `harness` 覆盖率报告（目标 ≥85%，当前未统计）
- [ ] 把 R3 的诊断计数整理成 `evidence/diagnostics.json`，与文档表格同源生成
- [ ] 为 CLI 增加 `gate-replay` 子命令，统一入口

## v1.1（已完成）

- [x] 执行器分层：`OfflineExecutor` / `OnlineExecutor`（并发、预留/结算、断点续跑）
- [x] OpenAI 兼容 runner：429/5xx/超时退避、usage 计费、JSON 模式
- [x] 反思提议器 + 预置 mock 提议器
- [x] `evolve()` 在线闭环、`resume(run_id)` 演化级续跑、自动报告
- [x] CLI：init / run / evolve / report + 原有凭证命令
- [x] 104 项测试（含本地 mock 端点的 8 路并发、429、超时、usage 缺失）

## v1.2（已完成，无需新预算）

- [x] 统计口径 `harness/stats.py`：Wilson、精确 McNemar（含连续性校正）、BCa、MDE
- [x] 门禁第三态：样本不足输出“证据不足”，不推进版本、凭证记 `insufficient`
- [x] 选择偏差 `harness/selection.py`：比较次数、有效比较、候选 > 3 的族错误率警告
- [x] 幂等与恢复：重复 accept 返回既有凭证；轮次内 5 个持久化断点均可续跑
- [x] 协议 `harness/protocol.py`：导入导出、`protocol_hash` 写入凭证、测试集只跑一次
- [x] 可观测 `harness/trace.py`：JSONL 事件日志 + CLI `trace` / `stats`
- [x] 并发与限流：令牌桶 + 尊重 `Retry-After`、SQLite WAL、`examples/bench_write.py`
- [x] 234 项测试（新增 stats / selection / protocol / trace / ratelimit / evidence / resume 等 130 项）

## v1.2.1（需要小额预算）

- [ ] R1 复跑：3 个种子 × 60 题，报告均值与种子间方差；仍不承诺显著性
- [ ] R2 结论复核：同一批题上重跑人工改进候选，确认 −6.67pp 不是单次服务波动
- [ ] 成本口径统一：记录每题 token 用量，替代延迟代理

## v1.3（能力扩展）

- [ ] 用真实端点跑一轮小样本闭环（≤60 题、≤¥5），公开逐轮结果与成本
- [ ] 执行器接入真实模型（增加显式 `mode="real"` 路径、预算硬上限、失败重试与账本），并把“无发送能力”的断言改为“默认离线、需显式开关”
- [ ] 凭证外部锚定：把链头写入公开时间戳服务或 CI 签名，使“整链重建”可被检测

## 社区与上游

- [ ] 向 GEPA 提交适配补丁（`best_score` 兼容、成本账本接口）——**计划中，未提交**
- [ ] 提供 `gepa` 版本检测：安装到 PyPI 版本时给出明确警告而不是失败 1 项测试
- [ ] 收集外部试用反馈（至少 3 个独立使用场景）后再谈“社区采用”

## 明确不做

- 不做通用优化器，不与 GEPA 争候选质量；
- 不提供“一键提升准确率”的承诺；
- 不把合成/预置结果包装成实测；
- 不在没有授权的情况下接入真实高校制度或学生数据。
