# 协议（ProtocolSpec）

协议回答一个问题：**这次实验到底允许做什么**。划分、评价器、阈值、重复次数、
随机种子、样本门槛和测试集策略一旦冻结，就写成一个对象；导出的 JSON 可以随论文
或评审材料一起提交，任何人用 `ProtocolSpec.load()` 重放都能得到同一个哈希。

代码在 `harness/protocol.py`，schema 为 `protocol/1`。

## 1. 字段

| 字段 | 含义 | 默认 |
| --- | --- | --- |
| `schema` | 固定 `protocol/1` | — |
| `protocol_id` | 协议标识（同一数据集可以有多个协议） | `"{dataset}-{profile}-v1"` |
| `dataset_id` | 数据集标识 | — |
| `val_task_ids` | 验证集清单（参与选择） | — |
| `test_task_ids` | 测试集清单（只汇报） | 允许为空 |
| `evaluator_id` | 评价器；只接受仓库内置打分器 | `hotpot-em-f1-v1` |
| `gate_profile` | `screen` / `release` / `custom` | `release` |
| `thresholds` | 门禁阈值覆盖（相对档位预设） | `{}` |
| `repeats` | 每条任务重复次数 | `1` |
| `seed` | bootstrap 随机种子 | `1` |
| `bootstrap_resamples` | 重采样次数（≥100） | `2000` |
| `confidence` | 区间置信水平 | `0.95` |
| `min_sample` | 低于该题数时门禁输出“证据不足” | `30` |
| `test_policy` | `once` / `report-only` | `once` |
| `created_at` | 创建时间（**不参与哈希**） | 自动 |

校验在构造时完成：验证集与测试集不得重叠、任务 ID 不得重复、`evaluator_id`
必须在白名单内、阈值必须非负、`require_positive_interval` 必须是布尔值。
未知字段一律拒绝，避免“看起来生效了其实没有”的配置。

## 2. 协议哈希

```
protocol_hash = SHA-256(canonical(to_dict() 去掉 created_at))
```

- 同一协议重复导出、跨机器导入都得到同一个哈希；
- `created_at` 之外的**任何**改动都会改变哈希——包括 `seed`、`repeats`、
  `min_sample`、`test_policy`；
- 决策凭证的 `protocol_hash` 字段指向它（见第 4 节）。

`canonical` 是 `json.dumps(ensure_ascii=False, sort_keys=True, separators=(",", ":"))`
的 UTF-8 编码，与仓库其余内容哈希一致。

## 3. 导入导出

```bash
# 从最近一次演化导出（推荐：导出的是实际执行过的协议）
python -m harness.cli protocol-export --db <状态目录> --out protocol.json

# 没有演化记录时，从已注册数据集生成
python -m harness.cli protocol-export --db <状态目录> --dataset data/dataset.json --out protocol.json

# 导入并校验；会写到 <状态目录>/protocol.json
python -m harness.cli protocol-import --db <状态目录> --file protocol.json

# 查看当前协议与警告
python -m harness.cli protocol-show --db <状态目录>
```

Python 侧：

```python
from harness import ProtocolSpec, build_protocol, protocol_warnings

protocol = build_protocol("ds", val_ids, test_ids, gate_profile="screen", min_sample=10)
print(protocol.hash)
protocol.save("protocol.json")
restored = ProtocolSpec.load("protocol.json")
assert restored.hash == protocol.hash

for warning in protocol_warnings(protocol):
    print("-", warning)
```

`protocol_warnings()` 不阻断执行，但**必须出现在报告里**：测试集小于
`min_sample`、`repeats > 1`、`test_policy=report-only` 各有一条；全部干净时输出
“协议检查通过：划分互斥、评价器可复算、种子与阈值已冻结”。

示例（节选）：

```json
{
  "schema": "protocol/1",
  "protocol_id": "campus-synthetic-screen-v1",
  "dataset_id": "campus-synthetic",
  "val_task_ids": ["v1", "v2", "v3"],
  "test_task_ids": ["t1", "t2"],
  "evaluator_id": "hotpot-em-f1-v1",
  "gate_profile": "screen",
  "thresholds": {
    "min_em_gain": 0.02, "max_f1_drop": 0.02, "max_regression_rate": 0.1,
    "max_cost_ratio": 2.0, "require_positive_interval": false
  },
  "repeats": 1, "seed": 1, "bootstrap_resamples": 2000,
  "confidence": 0.95, "min_sample": 30, "test_policy": "once",
  "created_at": "2026-10-29T12:00:00+00:00"
}
```

## 4. 凭证里的 protocol_hash

每次 accept / reject / insufficient 写入的凭证固定包含 `protocol_hash`：

- evolve 路径：取评价报告里的协议哈希（即本次演化实际使用的协议）；
- 非 evolve 路径：取候选登记时绑定的协议；两者都没有时为空字符串，含义是
  “该次决策未绑定协议”——字段仍存在，不会因缺失而改变凭证结构。

因此复核一条凭证时，可以回答“这条决策是在哪份协议下做出的”，以及“协议后来
有没有被改过”。

## 5. 测试集只跑一次（硬约束）

`test_policy=once` 由 `TestSetGuard` 执行：扫描状态库中所有 `completed` /
`running` 的运行记录，若存在**同一数据集 + 完全相同测试清单**的外部运行，直接抛
`StateConflict`，拒绝继续。

- 本演化自身产生的运行（`own_run_ids`）不计入，因此测试阶段的断点续跑不会被
  自己的守卫挡住；
- 判重只看“数据集 + 测试任务清单的精确序列”，换数据集或换划分都不算复用；
- `test_policy=report-only` 时不阻断，但把 `matching_runs` 记进报告的 guard 字段，
  让复用这件事**可见**。

守卫结果会写进 `test_metrics.guard`：

```json
{
  "protocol_hash": "f4b764f…",
  "policy": "once",
  "test_task_count": 8,
  "matching_runs": 0,
  "own_runs": 0,
  "reused": false
}
```

注意这是**流程约束**，不是统计学约束：它防止“多跑几次挑一个好看的结果”，不能
替代独立测试集。真正要复验结论，仍需要新的 holdout。

## 6. 与 evolve / resume 的关系

- `evolve(..., protocol=...)` 会校验协议与参数一致（数据集、验证/测试清单、档位），
  不一致直接 `ValueError`；
- 不传协议时自动 `build_protocol(dataset_id, val_ids, test_ids, gate_profile=...)`；
- 协议快照（`protocol.to_dict()`）与 `protocol_hash` 写进演化记录，`resume()`
  从记录里恢复**同一份协议**，不会在续跑时悄悄换种子或阈值；
- 测试阶段的 `base_run_id` / `final_run_id` 也持久化在 `test_phase`，续跑复用同一
  运行。

## 7. 边界

- 协议冻结的是**条件**，不是结论；协议相同不代表结果可跨模型比较。
- `thresholds` 只接受五个门禁字段；想改评价器或数据集必须新建协议（哈希随之改变）。
- `custom` 档位只表示“阈值不等于任何预设”，报告里会如实标注，不会冒充
  `screen` 或 `release`。
