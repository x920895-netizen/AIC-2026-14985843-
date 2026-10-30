# 本机接口与数据契约

运行后访问`/api/docs`及`/openapi.json`查看实际Pydantic输入与核心输出schema。所有字段以本机代码为准，输入禁止额外字段。接口错误400为无效参数，404为记录不存在，409为状态/完整性/预算冲突，422为输入schema错误，500为明确执行失败；失败不返回模拟成功。

| 接口 | 输入 | 主要输出 |
|---|---|---|
| GET /api/health | 无 | status、application、default_mode、real_enabled、disclaimer |
| GET /api/policies | 无 | documents（10条文档与元数据）、disclaimer |
| POST /api/chat | question；mode默认offline；confirm_paid、retain_trace、use_experiment默认false | ChatResult：answer、evidence、run_id、strategy_version、mode、api_calls、cost_cny、seconds、blocked_unsupported、disclaimer |
| POST /api/feedback | run_id、category（3种反馈）、note可选 | feedback_id、run_id、category、status=unreviewed、gold_eligible=false、created_at |
| GET /api/evolution/state | 无 | StateResult：当前策略、候选、评价、决策、版本、proposal轨迹、失败、服务指针、留出状态、费用与事件 |
| POST /api/evolution/propose | mode、confirm_paid；fixture仅离线指定improvement/regression | ProposalResult：candidate（登记记录）与proposal（结构化修改及来源） |
| POST /api/evolution/validate | candidate_id、mode、confirm_paid | EvaluationResult：passed、verdict、metrics、gates、reasons及运行/版本引用 |
| POST /api/evolution/accept | candidate_id | 已接受完整版本：version_id、parent_version_id、artifacts、config_snapshot、status及content_hash |
| POST /api/evolution/reject | candidate_id、reason | 拒绝decision_id、verdict、reasons、时间与候选引用 |
| POST /api/evolution/rollback | version_id、reason | 恢复的完整历史祖先版本 |
| POST /api/evolution/publish | version_id、confirm_review=true | published_version_id、human_reviewed、reviewed_at |
| POST /api/evolution/freeze | 无 | frozen_version_id、opened、independent |
| POST /api/evolution/holdout | mode、confirm_paid | n、strict、f1、run_id、mode、is_synthetic |
| POST /api/evolution/run-demo | 无 | 两个候选与评价、已接受版本、恢复检查、回滚版本、api_calls=0 |

AnswerResult只含facts、escalate、reason、conflicts；每个FactResult只含slot、value、citations。引用字符串为`doc_id:clause_id`，事实值匹配有效制度中的规范值。结构化输出防止未评分的额外自由文本混入服务回答。报告在mode及is_synthetic上区分模型执行方式和资料性质：即使真实模型调用，校园资料仍为合成。

所有应用状态操作通过SQLite事务保存；接受前校验运行、输出、账本及冻结条件。POST接口要求同源或本地工具请求；该机制不替代公网生产身份认证。仅面向localhost演示。
