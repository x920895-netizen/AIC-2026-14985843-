# 开源及第三方资源使用清单

本项目自研代码（`harness/`、`scripts/`、`tests/`、`examples/gate_replay.py`、校园示例中的应用层）采用 MIT 许可。第三方资源按各自许可保留，本清单不改变其权利归属。

| 资源 | 版本 / 来源 | 许可 | 用途与自主边界 | 随包内容 |
| --- | --- | --- | --- | --- |
| GEPA | 0.1.4；commit `7af5f0c5c79fcc16b2d5248b187d0864683edd4d` | MIT | 官方候选优化流程（R1/R2 的基线）；本地做过成本账本适配，未修改上游源码 | `upstream/gepa/`（含 LICENSE） |
| gepa-artifact | commit `cbefbc1aa0f43dd39874ec4bf42211365dbda42e` | MIT | 任务结构与反馈格式参考 | `upstream/gepa-artifact/`（含 LICENSE） |
| DSPy | 3.4.1 | MIT | 模块编排与 EM 判定 | `licenses/dspy-3.4.1.dist-info/` |
| bm25s | 0.2.12 | MIT | 两跳 BM25 检索 | `licenses/bm25s-0.2.12.dist-info/` |
| PyStemmer | 3.1.0 | MIT / BSD | 英文词干 | `licenses/pystemmer-3.1.0.dist-info/` |
| datasets | 5.1.0 | Apache-2.0 | 读取 HotpotQA 数据卡 | `licenses/datasets-5.1.0.dist-info/` |
| openai | 2.54.0 | Apache-2.0 | 兼容接口客户端 | `licenses/openai-2.54.0.dist-info/` |
| requests / psutil / cloudpickle | 2.34.2 / 7.2.2 / 3.1.2 | Apache-2.0 / BSD-3-Clause | 网络、系统信息、序列化 | `licenses/requests-2.34.2.dist-info/` 等 |
| numpy / scipy / pyarrow | 2.5.4 / 1.18.1 / 26.0.0 | BSD 系列（以发行 LICENSE 为准） | 数值计算与列式数据 | `licenses/numpy-2.5.4.dist-info/` 等 |
| FastAPI / Uvicorn / HTTPX | 0.115.12 / 0.34.2 / 0.28.1 | MIT / BSD-3-Clause | 校园示例的 Web 层（可选依赖） | `examples/campus_qa/licenses/`（15 个发行许可） |
| HotpotQA fullwiki | huggingface.co/datasets/hotpotqa/hotpot_qa | 数据卡 CC BY-SA 4.0 | 历史实验评测；**本包不再分发原文、参考文本或逐题账本**，只保留 ID、计数与聚合指标 | 无原文 |
| Wikipedia 2017 镜像 | 本地研究缓存 | 来源与镜像授权待核验 | 历史实验检索语料；**不再分发** | 无 |
| 阿里云百炼托管 API | `qwen3.8-flash` / `qwen3.8-max-0902` | 账号服务条款（非开放权重） | R1/R2/R3 的任务与反思推理；不含权重、不含凭据 | 无 |
| Pytest | 9.x | MIT | 测试运行器（开发依赖） | 未随包 |

## 分发边界

- 第三方源码只在 `upstream/` 保留 GEPA 与 gepa-artifact 两个 MIT 项目的原始副本，附带其 LICENSE。
- 其余依赖以 `licenses/*.dist-info/LICENSE*` 的形式保留发行许可文本，安装时由 pip 获取代码本身。
- HotpotQA 原文、Wikipedia 正文、模型原始响应与逐请求账本**均不在包内**。`runs/round3g/statistics.json`、`runs/round3h/statistics.json` 已替换为聚合摘要。
- 仓库不包含任何 API Key、账号、内部工作空间地址或个人目录路径。

## 许可兼容性说明

自研代码为 MIT，与上述 MIT/Apache-2.0/BSD 依赖兼容。HotpotQA 的 CC BY-SA 4.0 只约束其原文与衍生文本；本包不分发原文，因此不触发相同方式共享义务。若未来重新分发原文，必须单独评估并保留署名。
