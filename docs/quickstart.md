# 快速开始

## 环境

- Python 3.12（CI 与实验环境一致；`requires-python >= 3.10`）
- 联网一次用于安装依赖；之后跑测试、演示、门禁回放都不需要网络与 API Key
- Windows / macOS / Linux 均可，命令以 bash 为例

## 安装顺序（必须遵守）

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install ./upstream/gepa          # 第一步：仓库内的 GEPA
pip install -e ".[research]"         # 第二步：dspy / bm25s / PyStemmer
```

**为什么不能反过来？** PyPI 上的 `gepa` 0.1.4 与本仓库 `upstream/gepa` 版本号相同，但缺少 `best_score` 属性。先装 PyPI 版本会让 `tests/test_benchmark.py::test_gepa_uses_official_reflector_and_accepts_candidate` 失败，表现为 **32 passed / 1 failed**。先装本地源码包即可 33 passed。

CI 使用同样的顺序（`.github/workflows/ci.yml`）。

## 四个可复现入口

```bash
# 1. 测试（234 项：104 项 v1.1 累计 + 130 项 v1.2 统计/协议/恢复/限流等）
pytest tests/

# 2. 离线演示：6 个事件，api_calls=0
python scripts/demo_submission.py --output demo-output-01

# 3. 两级门禁历史回放（R1/R2 真实数字）
python examples/gate_replay.py --output evidence/gate_replay.json

# 4. 在线闭环演示（MockRunner，3 轮，1 拒 1 收，不联网）
python examples/evolve_demo.py

# 5. 复核演示产生的决策凭证链
python -m harness.cli verify-receipts --state-dir demo-output-01/state
```

演示目录必须**尚不存在**（`exist_ok=False`），重复运行请换一个目录名，避免覆盖历史记录。

## 命令一览

| 目的 | 命令 |
| --- | --- |
| 初始化状态目录 | `python -m harness.cli init --db <dir>` |
| 单次运行（mock/openai） | `python -m harness.cli run --db <dir> --dataset data.json --runner mock --output run.json` |
| 在线闭环 | `python -m harness.cli evolve --db <dir> --dataset data.json --proposer mock --runner mock --rounds 3 --budget 5` |
| 生成 Markdown 报告 | `python -m harness.cli report --db <dir> --out report.md` |
| 查看状态、候选、决策、凭证 | `python -m harness.cli inspect --db <dir>` |
| 列出凭证 | `python -m harness.cli receipts --db <dir>` |
| 校验哈希链（断链/篡改返回非零） | `python -m harness.cli verify-receipts --db <dir>` |
| 回滚到祖先 | `python -m harness.cli rollback --db <dir> --version version-... --reason "原因"` |
| 统计视图（Wilson/McNemar/BCa/MDE + 选择偏差） | `python -m harness.cli stats --db <dir>` |
| 事件日志摘要 | `python -m harness.cli trace --db <dir>`（`--raw` 逐行，`--event` 过滤） |
| 导出/导入协议 | `python -m harness.cli protocol-export --db <dir> --out protocol.json`、`protocol-import --file protocol.json` |
| 写入压测（1000 条） | `python examples/bench_write.py` |
| 校园示例 | `python examples/campus_qa/scripts/run_campus.py` 后打开 `http://127.0.0.1:8765` |
| 校园示例测试 | `pip install -e ".[campus]"` 后 `pytest examples/campus_qa/tests` |

安装后也可以直接用入口点：`evidence-evolve verify-receipts --db <dir>`；`python -m harness` 等价于 `python -m harness.cli`。

`--db` 与旧版的 `--state-dir` 等价，两个都可用。

## 常见问题

**`ModuleNotFoundError: No module named 'dspy'`**
演示脚本会在启动时给出提示并退出。执行 `pip install -e ".[research]"`。

**测试 32 passed / 1 failed**
装错 GEPA。执行：

```bash
pip install --force-reinstall --no-deps ./upstream/gepa
```

**`runs/` 下的报告脚本报“缺少本地原始账本”**
正常。`scripts/report_round1.py`、`scripts/report_round2.py`、`scripts/analyze_round2.py` 需要本地逐题/逐请求账本（`api_calls.jsonl`、`rollouts.jsonl`、`data/splits.json`），这些文件含第三方语料片段，未随包分发。脚本会列出缺失文件并以退出码 2 结束，不会抛 `FileNotFoundError`。

**安装时报 `PyStemmer` 编译失败**
PyStemmer 有 Windows/macOS 官方 wheel；Linux 需要 `python3-dev` 与 `g++`。

**能否只用核心包、不装 dspy？**
可以。`pip install -e .` 只安装 `harness`、`scripts` 与 CLI，核心零第三方依赖。但测试与演示需要独立评价器（DSPy 的 EM 实现），属于 `research` 额外依赖。
