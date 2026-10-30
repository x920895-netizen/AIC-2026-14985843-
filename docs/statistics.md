# 统计口径

本页说明 v1.2 起门禁、报告与 `stats` 命令使用的统计量：公式、默认参数、已知
分布的校验值，以及**什么情况下不能宣称结论**。所有函数都在
`harness/stats.py`，纯计算、不读状态、不联网；随机性只来自显式种子。

## 1. 为什么单独一页

同一个 +13.33pp 在大样本上可能是“几乎确定”，在 30 题上可能只是噪声。v1.1 已经
报告配对区间和 McNemar p，但把“样本够不够”“比了几个候选”留给读者判断。
v1.2 把这两件事变成**可计算、可复算、会出现在结论里**的量：

- 样本配不上结论 → 门禁输出第三态 `insufficient`（证据不足），不输出“失败”也不输出“通过”；
- 候选比较了 4 个以上 → 选择偏差摘要输出族错误率警告与 Bonferroni 建议 α。

## 2. 比例区间：Wilson score

给定 `k` 次成功、`n` 次观测、置信水平 `1-α`：

```
z = Φ⁻¹(1 - α/2)
center = (p̂ + z²/(2n)) / (1 + z²/n)
half   = z·√(p̂(1-p̂)/n + z²/(4n²)) / (1 + z²/n)
CI     = [max(0, center-half), min(1, center+half)]
```

不用 Wald 区间：当 `k=0` 或 `k=n` 时 Wald 宽度为 0，会给出“零误差”的假象；
Wilson 在小样本、接近 0/1 时端点仍在 `[0,1]` 内。`n=0` 返回 `[0,1]`，并在
`point` 字段返回 `null`——没有样本就没有点估计。

调用：`wilson_interval(successes, n, confidence=0.95)`。

## 3. 配对二分类：精确 McNemar

记 `b = fixed`（由错变对），`c = regressed`（由对变错）。默认使用**精确二项检验**
（双侧）：

```
p_exact = min(1, 2 · Σ_{k=0}^{min(b,c)} C(b+c, k) / 2^(b+c))
```

`continuity=True` 时改用**带连续性校正的卡方近似**（1 自由度）：

```
χ²_c = (|b-c| - 1)² / (b+c)
p_continuity = erfc(√(χ²_c / 2))
```

两者总是同时返回：`p_value` 是调用方选中的那个，`p_exact` / `p_continuity` 是另一
口径。不一致对数 `< 25` 时 `exact_recommended=True`，此时卡方近似不可靠，报告应
采用精确值。`b+c=0` 时 `p=1`，`statistic=0`。

调用：`mcnemar_exact(fixed, regressed, continuity=False)`。

## 4. 差值区间：百分位 bootstrap 与 BCa

配对差值 `d_i = candidate_i - parent_i`（i 为 `(task_id, repeat)`）。

- **百分位区间**（`percentile_interval`）：与 v1.1 门禁完全一致的口径——
  `resamples=2000`、`seed=1`、下/上索引 `int(0.025·B)-1 = 49` 与
  `int(0.975·B)-1 = 1949`。门禁仍只消费这个区间，所以**历史回放决策不变**。
- **BCa**（`bca_bootstrap`）：偏差校正 + 加速。Z0 用
  `Φ⁻¹((#{boot < obs} + 0.5·#{boot == obs}) / B)`，并列值按半分计，避免二值差值把
  Z0 压到 ±∞；加速项用 jackknife：

```
a = Σ(θ̄ - θ_i)³ / (6 · [Σ(θ̄ - θ_i)²]^(3/2))
q_lo = Φ(z0 + (z0 + z_α) / (1 - a(z0 + z_α)))
q_hi = Φ(z0 + (z0 + z_{1-α}) / (1 - a(z0 + z_{1-α})))
CI   = [Q_boot(q_lo), Q_boot(q_hi)]
```

两种区间都写进评价报告（`interval` 与 `interval_bca`）与 `stats` 视图。`n<2` 时
BCa 退化为点估计并标记 `degenerate=True`。

## 5. 最小可检测效应（MDE）

| 量 | 公式 | 用途 |
| --- | --- | --- |
| `mde_proportions(n, baseline, discordance)` | `(z_{1-α/2} + z_power)·√(p_disc/n)` | 这个样本量能检出多大的绝对提升 |
| `required_pairs(effect, baseline, discordance)` | `⌈((z_{1-α/2}+z_power)/effect)²·p_disc⌉` | 要检出目标效应需要多少配对题 |
| `power_at(n, effect, discordance)` | `Φ(effect/√(p_disc/n) - z_{1-α/2})` | 给定题数时的近似把握度 |

- 默认 `power=0.8`、`confidence=0.95`；
- `discordance` 未给出时取独立假设上界 `2·p(1-p)`（保守），并在结果里标
  `discordance_source = "independent-upper-bound"`；
- 门禁里的 `discordance` 取自实际不一致对数 `(fixed+regressed)/n`。

`analyze_paired` 把上述量组合成一份可复核报告：`n`、`fixed/regressed`、
`unchanged_right/unchanged_wrong`、`mcnemar`、`percentile`、`bca`、
`wilson_parent`、`wilson_candidate`、`mde`、`notes`、`underpowered`、
`sample_insufficient`、`delta_significant`。

## 6. 门禁第三态：证据不足

`evaluate_gate(metrics, spec) -> (passed, reasons)` **保持二值、逐字不变**，历史回放
与既有凭证因此不受影响。新增 `assess_gate` 在它之上给出三态：

| 条件 | 判定 |
| --- | --- |
| 全部判据通过 | `pass`（题数低于 `min_sample` 时附“探索性”注记） |
| 命中结构性硬否决（F1 退化、逐题回归率、成本比） | `fail` |
| 只命中统计判据 + 题数 `< min_sample` + 观测增益 > 0 | `insufficient` |
| 其他（含零增益、负增益、样本充足但不显著） | `fail` |

规则的理由：小样本 + 正向苗头既不该被当成“通过”，也不该被写成“已证明无效”；
它只说明**还需要更多题**。硬否决不会被小样本洗白——成本、F1、回归率是结构问题，
和题数无关。

`evidence` 字段：

```json
{
  "count": 6, "min_sample": 30, "underpowered": true,
  "observed_delta": 0.3333, "observed_abs_delta": 0.3333,
  "mde": 0.6603, "mde_pp": 66.03, "mde_discordance": 0.3333,
  "below_mde": true,
  "hard_vetoes": [], "statistical_vetoes": ["配对区间不足以支持正增益"],
  "notes": ["样本不足（n=6 < 30）：结论只作探索性证据", "观测效应 33.33pp 低于该样本 MDE 66.03pp"]
}
```

`insufficient` 的候选**不会**推进 current 指针，凭证 `decision` 记为
`insufficient`，并在演化记录的 `insufficient` 列表里单独列出。

## 7. 选择偏差

每一轮“提议 → 验证 → 判定”是一次比较。`harness/selection.py` 记账：

| 字段 | 含义 |
| --- | --- |
| `comparisons` | 实际产生比较的轮次（`stop` / `error` 不计） |
| `effective_comparisons` | 有可用指标、可能改变版本的比较 |
| `ineffective_comparisons` | 运行失败或缺指标的轮次 |
| `candidate_counts` | 每个候选被比较了几次（续跑重验会 > 1） |
| `version_counts` | 每个版本作为父/子参与了几次 |
| `distinct_candidates` | 不同候选数——**警告只由它触发** |
| `bonferroni_alpha` | `α / max(distinct_candidates, effective_comparisons)` |
| `family_wise_warning` | 候选数 > 3 时的可执行建议 |

判据：`distinct_candidates <= 3` 视为探索性比较，不警告；`> 3` 输出

```
已比较 5 个候选（>3 个，有效比较 5 次），族错误率未校正：
单次 α=0.05 下至少一次假阳性的上界约 0.25；
建议按 Bonferroni 使用 α=0.0100，或改用独立 holdout 复验
```

**版本数不参与触发**：同一父版本被多轮使用是正常演化，不是多做了独立假设检验。
同一候选被重复验证（断点续跑）会计入 `candidate_counts`，但不重复计候选数。

## 8. 已知分布校验（测试锁定的数值）

`tests/test_stats.py` 用独立实现给出的参考值锁定实现，容差 **±1% 相对误差**：

| 检查 | 参考 | 结果 |
| --- | --- | --- |
| Wilson 50/100 | [0.403832, 0.596168] | 一致 |
| Wilson 3/10 | [0.107791, 0.603222] | 一致 |
| Wilson 0/20 | [0, 0.161125] | 一致 |
| 精确 McNemar (12,3) | p=0.03515625 | 一致 |
| 精确 McNemar (8,2) | p=0.109375 | 一致 |
| 连续性校正 (12,3) | p=0.0388671 | 一致 |
| BCa vs 正态理论区间（n=400） | 相对误差 < 0.2% | 一致 |
| 百分位区间 vs 正态理论（n=400） | 相对误差 < 0.2% | 一致 |
| BCa 覆盖率（N(0,1)，n=30，200 次重抽） | ≥ 92%（名义 95%） | 通过 |

有 scipy 的环境会额外用 score equation 反解 Wilson 端点做交叉复核；没有 scipy
时该用例自动跳过，其余用例仍可运行。

复算：

```bash
py -3 -m pytest tests/test_stats.py -q
py -3 -m harness.cli stats --db <状态目录>
```

## 9. 边界

- 精确 McNemar 的 p 值基于二项分布，不做多重比较校正；族错误率只在选择偏差
  摘要里提示，门禁本身**不会**自动切换 Bonferroni 阈值（那会改变历史决策）。
- BCa 需要 `resamples >= 100`；默认 2000。二值差值并列多，BCa 与百分位区间通常
  很接近，报告两者是为了可审计，不是为了“挑一个更好看的”。
- MDE 是正态近似，题数极少（n<10）时只能当数量级参考。
- 统计显著不等于实际有用：`delta_significant` 与 `underpowered` 必须一起读。
