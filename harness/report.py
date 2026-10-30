"""从 SQLite 状态生成 Markdown 报告：版本链、门禁理由、凭证与成本。"""

from datetime import datetime, timezone
from pathlib import Path

from .receipt import ReceiptLedger, verify_chain


def _short(value: str | None, keep: int = 12) -> str:
    if not value:
        return "—"
    return value if len(value) <= keep else value[:keep]


def _pct(value) -> str:
    return "—" if value is None else f"{value*100:.2f}%"


def _pp(value) -> str:
    return "—" if value is None else f"{value*100:+.2f}pp"


def _cny(value) -> str:
    return "—" if value is None else f"¥{value:.4f}"


def _reasons(reasons) -> str:
    return "；".join(reasons) if reasons else "—"


def _version_chain(store, current_id: str) -> list[dict]:
    chain, seen = [], set()
    cursor = current_id
    with store.connection() as conn:
        while cursor and cursor not in seen:
            seen.add(cursor)
            version = store.get_version(conn, cursor, verify_artifacts=False)
            chain.append(version)
            cursor = version["parent_version_id"]
    return chain


def generate(store, *, evolution_run_id: str | None = None, title: str = "循证演化报告") -> str:
    versions = store.list("versions")
    candidates = store.list("candidates")
    evaluations = store.list("evaluations")
    decisions = store.list("decisions")
    budgets = store.list("budgets")
    runs = store.list("runs")
    evolutions = store.list("evolution_runs")
    with store.connection() as conn:
        receipts = ReceiptLedger(store).all(conn)
        try:
            chain = verify_chain(conn)
        except Exception as exc:  # 报告不能因为链损坏而崩溃，必须把问题写出来
            chain = dict(verified=False, error=str(exc), count=0, head="")
    current = store.current()
    evolution = None
    if evolution_run_id:
        evolution = next((e for e in evolutions if e["run_id"] == evolution_run_id), None)
    elif evolutions:
        evolution = sorted(evolutions, key=lambda e: e["updated_at"])[-1]

    lines = [f"# {title}", "",
             f"- 生成时间：{datetime.now(timezone.utc).isoformat()}",
             f"- 当前版本：`{current['version_id']}`（{current['status']}）",
             f"- 候选：{len(candidates)}　版本：{len(versions)}　凭证：{len(receipts)}",
             ""]

    lines += ["## 版本链", "",
              "| 版本 | 状态 | 父版本 | 创建时间 | 决策 |",
              "| --- | --- | --- | --- | --- |"]
    for version in _version_chain(store, current["version_id"]):
        lines.append(f"| `{_short(version['version_id'], 18)}` | {version['status']} | "
                     f"`{_short(version['parent_version_id'], 18)}` | {version['created_at']} | "
                     f"{len(version.get('decision_ids', []))} |")
    lines.append("")

    lines += ["## 候选与门禁理由", "",
              "| 候选 | 目标 | 状态 | 父版本 | EM 变化 | 门禁理由 |",
              "| --- | --- | --- | --- | ---: | --- |"]
    evaluation_by_candidate = {e["candidate_id"]: e for e in evaluations}
    for candidate in sorted(candidates, key=lambda c: c["created_at"]):
        report = evaluation_by_candidate.get(candidate["candidate_id"], {})
        metrics = report.get("metrics") or {}
        lines.append(f"| `{_short(candidate['candidate_id'], 18)}` | {candidate['target']} | "
                     f"{candidate['status']} | `{_short(candidate['parent_version_id'], 18)}` | "
                     f"{_pp(metrics.get('delta_em'))} | {_reasons(report.get('reasons'))} |")
    lines.append("")

    if evolution:
        rounds = evolution.get("rounds") or []
        lines += ["## 演化轮次", "",
                  f"- 演化 ID：`{evolution['run_id']}`",
                  f"- 门禁档位：{evolution['gate_profile']}　轮次：{len(rounds)}/{evolution['max_rounds']}",
                  f"- 停止原因：{evolution.get('stop_reason')}",
                  f"- 接受 / 拒绝：{len(evolution.get('accepted', []))} / {len(evolution.get('rejected', []))}",
                  "", "| 轮 | 候选 | 决策 | EM 前 | EM 后 | 变化 | F1 变化 | 回归率 | 成本比 | 理由 |",
                  "| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
        for row in rounds:
            m = row.get("metrics") or {}
            ratio = "—" if m.get("cost_ratio") is None else "%.4f" % m["cost_ratio"]
            lines.append(f"| {row['round']} | `{_short(row['candidate_id'], 14)}` | {row['decision']} | "
                         f"{_pct(m.get('parent_em'))} | {_pct(m.get('candidate_em'))} | {_pp(m.get('delta_em'))} | "
                         f"{_pp(m.get('delta_f1'))} | {_pct(m.get('regression_rate'))} | "
                         f"{ratio} | {_reasons(row.get('reasons'))} |")
        lines.append("")
        val = evolution.get("val_metrics") or {}
        test = evolution.get("test_metrics") or {}
        lines += ["## 最终指标", "",
                  "### 验证集（参与选择）", "",
                  f"- 题目数：{val.get('count')}　EM：{_pct(val.get('em'))}　F1：{_pct(val.get('f1'))}",
                  f"- 成本：{_cny(val.get('cost_cny'))}（真实 {_cny(val.get('actual_cost_cny'))}，"
                  f"API 调用 {val.get('api_calls')}）", ""]
        lines += ["### 测试集（不参与选择）", ""]
        if test.get("error"):
            lines += [f"- 未完成：{test['error']}（{test.get('reason')}）", ""]
        else:
            base = test.get("baseline") or {}
            cand = test.get("candidate") or {}
            lines += ["| 版本 | 题数 | EM | F1 |", "| --- | ---: | ---: | ---: |",
                      f"| 基线 `{_short(base.get('version_id'), 18)}` | {base.get('count')} | "
                      f"{_pct(base.get('em'))} | {_pct(base.get('f1'))} |",
                      f"| 最终 `{_short(cand.get('version_id'), 18)}` | {cand.get('count')} | "
                      f"{_pct(cand.get('em'))} | {_pct(cand.get('f1'))} |",
                      "", f"- EM 变化：{_pp(test.get('delta_em'))}；F1 变化：{_pp(test.get('delta_f1'))}",
                      f"- 测试集是否参与选择：{'是' if test.get('selection_used_test') else '否'}", ""]

    lines += ["## 决策凭证", "",
              f"- 链校验：{'通过' if chain.get('verified') else '失败'}　条数：{chain.get('count')}　"
              f"链头：`{_short(chain.get('head'), 24)}`",
              ""]
    if receipts:
        lines += ["| 序号 | 凭证 | 决策 | 候选 | 档位 | 时间 | 哈希 |",
                  "| ---: | --- | --- | --- | --- | --- | --- |"]
        for index, receipt in enumerate(receipts, 1):
            lines.append(f"| {index} | `{_short(receipt['receipt_id'], 16)}` | {receipt['decision']} | "
                         f"`{_short(receipt['candidate_id'], 14)}` | {receipt['profile']} | "
                         f"{receipt['created_at']} | `{_short(receipt['receipt_hash'], 16)}` |")
        lines.append("")

    lines += ["## 成本", "",
              "| 预算作用域 | 上限 | 已用 | 模拟费用 | 真实费用 | API 调用 | 状态 |",
              "| --- | ---: | ---: | ---: | ---: | ---: | --- |"]
    for scope in budgets:
        lines.append(f"| `{_short(scope['scope_id'], 16)}` | {_cny(scope['limit_cny'])} | "
                     f"{_cny(scope['accounted_cny'])} | {_cny(scope.get('simulated_cny'))} | "
                     f"{_cny(scope.get('real_cny'))} | {scope.get('api_calls', 0)} | {scope['status']} |")
    lines += ["", f"- 运行数：{len(runs)}；凭证数：{len(receipts)}", ""]
    return "\n".join(lines)


def write(store, path, *, evolution_run_id: str | None = None, title: str = "循证演化报告") -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(generate(store, evolution_run_id=evolution_run_id, title=title), encoding="utf-8")
    return target
