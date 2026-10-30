# 贡献指南

感谢愿意改进这个项目。请先读 [README](README.md) 的“证据边界”与 [docs/limitations.md](docs/limitations.md)：**任何让结论看起来比证据更强的改动都不会被合并**。

## 开发环境

```bash
python -m venv .venv
source .venv/bin/activate
pip install ./upstream/gepa        # 必须先装
pip install -e ".[research,dev]"
pytest tests/
```

Windows 下把 `source .venv/bin/activate` 换成 `.venv\Scripts\activate`。

## 提交前自检

1. `pytest tests/` 全绿（当前 234 项）；
2. `python examples/gate_replay.py --output /tmp/gate_replay.json` 退出码为 0；
3. `python scripts/demo_submission.py --output /tmp/demo-$$` 6 个事件全部通过；
4. `python examples/evolve_demo.py` 3 轮闭环、1 拒 1 收、凭证链通过；
5. 没有新增真实 API 调用、密钥、本机绝对路径或第三方语料原文；
6. 新数字必须能落到 `evidence/` 里的某个文件，并在 PR 描述里写出复算命令。

## 代码约定

- Python ≥3.10，标准库优先；核心 `harness` 不引入第三方运行时依赖。
- 中文注释与错误信息，措辞与现有门禁理由保持一致（`主指标EM增益不足` 等）。
- 哈希一律走 `harness.contracts.canonical` / `content_hash`，不要自己拼 JSON。
- 涉及状态写入的代码必须放在 `store.transaction(...)` 内；新增检查点用 `store.checkpoint("名字")`，便于故障注入测试。
- 不要修改 `upstream/` 下的第三方源码；需要适配时在 `harness/` 或 `scripts/` 里包一层，并在 `THIRD_PARTY.md` 说明。

## 测试约定

- 新功能至少带正向与失败路径测试；
- 涉及门禁的改动必须同时更新 `tests/test_gate_replay.py` 的锁定值，并在 PR 中解释数字为何变化；
- 涉及凭证字段的改动必须更新 `RECEIPT_FIELDS` 与 `tests/test_receipts.py`，并说明兼容性（旧库是否需要迁移）。

## 提交信息

使用祈使句，首行 ≤72 字符，例如：

```
gate: 为 screen 档补充区间豁免测试

release 档仍要求配对区间下界 >0；screen 档不要求。
新增 3 个边界用例，gate_replay 输出不变。
```

## PR 流程

1. 从 `main` 切分支；
2. 填写 PR 模板（改了什么、为什么、如何验证、证据边界是否变化）；
3. CI 必须全绿；
4. 涉及对外数字的改动需要第二位维护者复核。

## 报告问题

- 安全漏洞：见 [SECURITY.md](SECURITY.md)，不要开公开 Issue；
- 结果复现失败：请附 `python -V`、`pip freeze` 中 `gepa`/`dspy` 版本、完整报错与运行命令；
- 文档表述夸大：直接开 Issue，标注文件与行号，这属于高优先级缺陷。

## 行为准则

参与本项目即表示同意 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
