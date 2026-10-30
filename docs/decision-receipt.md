# 决策凭证

## 它解决什么问题

门禁给出“接受/拒绝”之后，证据链通常断在日志里：谁、在什么条件下、基于哪份指标做的决定，事后无法离线复核。决策凭证把结论固化成一条哈希链记录，任何人拿到同一个 SQLite 文件就能验证“有没有被改过、有没有被删过、顺序有没有被动过”。

凭证**不复制原始输出**，只记录指纹，因此可以随包分发而不泄露语料。

## 字段

| 字段 | 含义 |
| --- | --- |
| `receipt_id` | `receipt-<uuid4hex>`，唯一 |
| `created_at` | UTC ISO-8601 |
| `decision` | `accept` / `reject` |
| `candidate_id` | 候选 ID |
| `parent_version` | 候选的父版本 ID |
| `candidate_version` | 候选生成的版本 ID |
| `profile` | `screen` / `release` / `custom`（由阈值反推，不冒充预设） |
| `gate_spec_hash` | `GateSpec.to_dict()` 的 SHA-256 |
| `diff_hash` | 候选 unified diff 文本的 SHA-256 |
| `metrics_hash` | 评价报告 metrics 的 SHA-256（否决时为空对象 `{}` 的哈希） |
| `evidence_hash` | 运行 ID、条件哈希、任务清单、版本哈希与否决理由的 SHA-256 |
| `prev_receipt_hash` | 上一条凭证的 `receipt_hash`；首条为 `""` |
| `receipt_hash` | 除自身外全部字段的 SHA-256 |

## 哈希规则

规范化 JSON 与全仓库一致：

```python
json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
```

再取 `sha256(...).hexdigest()`（小写 64 位十六进制）。

`receipt_hash = sha256(canonical({k: v for k, v in receipt.items() if k != "receipt_hash"}))`。

这意味着：

- 字段顺序、空白、非 ASCII 转义都不影响结果；
- 改任何一个字段都必须同时改 `receipt_hash`，而改 `receipt_hash` 又会破坏下一条的 `prev_receipt_hash`；
- 想整条替换，还必须重算后续所有凭证，否则链断。

## 写入时机

| 场景 | 写入点 | 说明 |
| --- | --- | --- |
| 门禁否决 | `validate()` 事务内 | 报告、决策、候选状态、拒绝凭证同时提交 |
| 接受 | `accept()` 事务内 | 版本状态、指针切换、决策、接受凭证同时提交 |
| 显式拒绝 | `reject(candidate_id, reason)` | 人工或策略否决；`registered` / `validated` 状态可拒绝 |

凭证与发布在**同一个 `BEGIN IMMEDIATE` 事务**里，检查点注入故障时一起回滚（`tests/test_receipts.py::test_receipt_is_written_in_the_same_transaction_as_the_decision`）。

同一 `(candidate_id, decision)` 只能出现一次，重复写入抛 `StateConflict`。

## 校验

```python
from harness.receipt import verify_chain

with store.connection() as conn:
    result = verify_chain(conn)   # {'verified': True, 'count': 2, 'head': '...'}
```

命令行：

```bash
python -m harness.cli verify-receipts --state-dir demo-output-01/state
```

`verify_chain` 依次检查：

1. 字段集合恰好等于 `RECEIPT_FIELDS`（缺字段或多字段都失败）；
2. `receipt_hash` 与重算值一致（内容被改）；
3. 存储的 `digest` 与 `receipt_hash` 一致（存储层被改）；
4. `prev_receipt_hash` 等于上一条的 `receipt_hash`（断链）。

任一失败抛 `IntegrityError`，CLI 返回非零退出码。

## 威胁模型

**能证明**：凭证链自写入后未被单点篡改；顺序未被调换；每条凭证对应的门禁条件与指标在写入时是确定的。

**不能证明**：

- **不能证明指标本身算对了**。指标由独立评价器重算，凭证只绑定其哈希；要验证指标需回到 `runs/` 与评价器。
- **不能防“从头重建整条链”**。如果攻击者能同时改写所有凭证、重算全部哈希并替换数据库，链会自洽。要防这一点需要外部锚定（签名、时间戳服务、公开日志），本版本未实现。
- **不提供身份认证**。`created_at` 是本地时钟，`receipt_id` 是 UUID，不构成“谁做的”证明；身份需要仓库的提交签名或 CI 记录。
- 凭证不承诺“拒绝理由一定正确”，只承诺“这条理由在写入时就是这个值”。

## 字段稳定性

`tests/test_receipts.py` 锁定了：

- 字段集合与顺序无关但集合固定；
- 五个哈希字段均为 64 位小写十六进制；
- 相同输入（含固定 `receipt_id`、`created_at`）产生完全相同的凭证；
- 修改内容后不重算哈希必定被检测；
- 伪造“自洽”的凭证（重算自身哈希）会被下一条的 `prev_receipt_hash` 拒绝。
