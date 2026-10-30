# 校园智服（第二场景示例）

把同一套 harness 用到“校园缓考制度问答”上的离线演示。它证明治理底座可以迁移到另一个评价协议，**不代表真实校园问答质量**。

## 状态（请先读）

- 10 份**合成**制度，36 道**合成**题（12 提议 / 12 验证 / 12 留出）。
- 全部为确定性离线解释器：`api_calls=0`、真实费用 0。
- **没有真实高校授权、没有学生试用、没有公网部署、没有真实用户。**
- 三组题目是同类意图的措辞变体，语义独立性有限，不证明真实泛化。
- 演示结果 11/12 → 12/12（修复 1、回归 0）由预置规则补丁产生，不是大模型反思收益。

## 依赖

```bash
pip install -e ".[campus]"        # fastapi==0.115.12 / uvicorn==0.34.2 / httpx==0.28.1
```

校园应用复用仓库根目录的 `harness` 包（不是副本），因此必须在仓库根目录或已 `pip install -e .` 的环境中运行。

## 启动

```bash
python examples/campus_qa/scripts/run_campus.py
# 打开 http://127.0.0.1:8765
```

默认只监听 `127.0.0.1`、离线模式。`--allow-real` 会允许页面逐次确认后付费调用（启动本身不发送请求）；没有 API Key 时该路径不可用。

## 测试

```bash
pytest examples/campus_qa/tests
```

34 项通过 / 39 subtests / 约 53 秒（含一次跨进程重启恢复）。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查，返回默认模式与免责声明 |
| GET | `/api/policies` | 合成制度列表（带 CP03/CP04/CP05 条款号） |
| POST | `/api/chat` | 结构化事实回答 + 证据引用；默认离线 |
| GET | `/api/evolution/state` | 当前版本、组件、候选、评价、决策、凭证 |
| POST | `/api/evolution/propose` | 提议候选（improvement / regression 两个 fixture） |
| POST | `/api/evolution/validate` | 独立验证并给出门禁结论 |
| POST | `/api/evolution/accept` / `reject` | 原子接受 / 显式拒绝 |
| POST | `/api/evolution/rollback` | 回滚到祖先版本 |
| POST | `/api/evolution/run-demo` | 完整闭环（重复调用返回 409） |
| GET | `/api/research` | 研究边界、已实现能力、未验证项 |

完整字段以 `/api/docs` 的 OpenAPI schema 为准。

## 目录

```
campus_app/      应用层：数据、检索、答案协议、评价器、服务、API、静态页面
tests/           34 项应用与协议测试
scripts/         启动入口
docs/            启动、接口、隐私与安全说明
licenses/        Web 依赖的发行许可
```

## 与主包的关系

- 复用 `harness` 的候选登记、工件校验、预算账本、事务接受、祖先回滚；
- 新增 `campus-policy-v1` 评价协议（`GateSpec.evaluator_id` 白名单），评价器 `campus_app/evaluation.py` 计算严格正确率、事实 F1、引用覆盖与安全性；
- 校园门禁是独立的一组检查项（严格正确率、F1、回归、引用覆盖、无依据比例、安全性、成本），与 HotpotQA 的 EM/F1 分数**不可比较**。
