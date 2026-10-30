"""端到端在线闭环演示（两个场景，不联网、不需要 API Key）。

场景 A「基本闭环」：12 道合成 QA（6 验证 / 6 测试），门禁档位 screen。
初始指令答对 3/6；``bad_patch`` 后 2/6 被拒；``good_patch`` 后 5/6 被接受；
第 3 轮提议器不再给出改动 → 停止。

场景 B「选择偏差」：16 道合成 QA（8 验证 / 8 测试），连续比较 5 个候选。
候选数超过 3 时选择偏差摘要会输出族错误率警告（Bonferroni 建议 α），
并明确报告“有效比较次数”。两个场景都只在最后跑一次测试集。

所有调用都走预算账本与 SQLite，``external_api=false``，费用为模拟费用。
"""

import argparse
import json
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/"examples"))

from harness import EvolutionFoundation, FoundationStore, RuntimeTask  # noqa: E402
from harness.proposers.mock import ScriptedProposer  # noqa: E402
from harness.receipt import verify_chain  # noqa: E402
from harness.report import write as write_report  # noqa: E402
from harness.runners.mock import MockRunner  # noqa: E402
from harness.trace import TraceLog  # noqa: E402

ARTIFACTS = {
    "prompt": {"components": {"final_answer.predict": "请回答校园制度问题，只输出最终答案。"}},
    "memory": {"experience_ids": [], "retrieval_policy": {}, "lifecycle_policy": {}},
    "tool": {"tools": [], "selection_policy": {}, "risk_constraints": {}},
    "skill": {"definitions": [], "selection_policy": {}, "lifecycle_policy": {}},
}
STRATEGIES = ("bad_patch2", "good_patch2", "bad_patch", "good_patch", "small_patch")
MARKER = re.compile(r"<!-- (" + "|".join(STRATEGIES) + r")")


def build_state(root: Path, tasks_rows, labels):
    store = FoundationStore(root)
    engine = EvolutionFoundation(store)
    refs = {kind: store.write_artifact(kind, value) for kind, value in ARTIFACTS.items()}
    store.initialize(refs, {"model_config": {"model": "mock-deterministic", "temperature": 0}},
                     experiment_config={"study_id": "evolve-demo"})
    tasks = [RuntimeTask(task_id, f"背景：校园缓考与重修规定（合成制度）。问题：{question}")
             for task_id, question, _ in tasks_rows]
    engine.register_dataset("campus-synthetic", tasks, labels)
    return store, engine


def fmt(value, kind="pp"):
    if value is None:
        return "—"
    return f"{value*100:+.2f}pp" if kind == "pp" else f"{value*100:.2f}%"


# ---------------------------------------------------------------------------
# 场景 A：基本闭环
# ---------------------------------------------------------------------------

TASKS_A = [
    ("v1", "缓考申请最迟什么时候提交？", "考试前 3 个工作日"),
    ("v2", "缓考需要哪些材料？", "申请表与医院证明"),
    ("v3", "缓考由谁审批？", "学院教务办"),
    ("v4", "缓考成绩如何记载？", "记入补考成绩"),
    ("v5", "未按时提交会怎样？", "按缺考处理"),
    ("v6", "缓考可以申请几次？", "每门课一次"),
    ("t1", "重修申请在哪个系统提交？", "教务系统"),
    ("t2", "重修是否需要缴费？", "需要按学分缴费"),
    ("t3", "重修成绩上限是多少？", "60 分"),
    ("t4", "重修可以跨学院吗？", "需开课学院同意"),
    ("t5", "重修与补考冲突吗？", "不能同时参加"),
    ("t6", "重修申请截止时间？", "开学两周内"),
]
LABELS_A = {task_id: gold for task_id, _, gold in TASKS_A}
SOLVED_A = {
    "base": {"v1", "v2", "v3", "t1", "t2", "t3"},
    "bad": {"v1", "v2", "t1", "t2"},
    "good": {"v1", "v2", "v3", "v4", "v5", "t1", "t2", "t3", "t4", "t5"},
}


def variant_a(prompt: str) -> str:
    if "先逐跳确定" in prompt:
        return "good"
    if "回答尽量简短" in prompt:
        return "bad"
    return "base"


def responder_a(task, prompt, artifacts):
    solved = SOLVED_A[variant_a(prompt)]
    return LABELS_A[task.task_id] if task.task_id in solved else "无法确定"


# ---------------------------------------------------------------------------
# 场景 B：选择偏差（5 个候选）
# ---------------------------------------------------------------------------

TASKS_B = [
    ("v1", "补考报名在哪里提交？", "教务系统"),
    ("v2", "补考是否需要申请？", "需要提前申请"),
    ("v3", "补考成绩如何记载？", "记入补考成绩"),
    ("v4", "补考缺考会怎样？", "按缺考处理"),
    ("v5", "补考可以申请几次？", "每门课一次"),
    ("v6", "补考与重修冲突吗？", "不能同时参加"),
    ("v7", "补考申请截止时间？", "考试前 3 个工作日"),
    ("v8", "补考需要缴费吗？", "不需要缴费"),
    ("t1", "缓考成绩上限是多少？", "60 分"),
    ("t2", "缓考跨学院需要同意吗？", "需开课学院同意"),
    ("t3", "缓考可以申请几次？", "每门课一次"),
    ("t4", "缓考未按时提交会怎样？", "按缺考处理"),
    ("t5", "缓考材料包括什么？", "申请表与医院证明"),
    ("t6", "缓考由谁审批？", "学院教务办"),
    ("t7", "缓考成绩如何记载？", "记入补考成绩"),
    ("t8", "缓考最迟何时提交？", "考试前 3 个工作日"),
]
LABELS_B = {task_id: gold for task_id, _, gold in TASKS_B}
VAL_B = [task_id for task_id, _, _ in TASKS_B if task_id.startswith("v")]
TEST_B = [task_id for task_id, _, _ in TASKS_B if task_id.startswith("t")]
#: 每个补丁的可答对集合；父版本按“最后一处补丁”判定，保证结果可复算。
SOLVED_B = {
    "base": {"v1", "v2", "v3", "t1"},
    "bad_patch": {"v1", "v2"},
    "good_patch": {"v1", "v2", "v3", "v4", "v5", "v6", "t1", "t2"},
    "bad_patch2": {"v1"},
    "good_patch2": set(VAL_B) | {"t1", "t2", "t3", "t4", "t5", "t6"},
    "small_patch": {"v1", "v2", "v3", "v4", "t1"},
}
STEPS_B = ["bad_patch", "good_patch", "bad_patch2", "good_patch2", "small_patch"]


def variant_b(prompt: str) -> str:
    found = MARKER.findall(prompt)
    return found[-1] if found else "base"


def responder_b(task, prompt, artifacts):
    solved = SOLVED_B[variant_b(prompt)]
    return LABELS_B[task.task_id] if task.task_id in solved else "无法确定"


# ---------------------------------------------------------------------------
# 输出辅助
# ---------------------------------------------------------------------------

def print_table(rounds):
    print("| 轮次 | 决策 | 候选 | EM 前 | EM 后 | 变化 | F1 变化 | 回归率 | 否决理由 |")
    print("| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |")
    for row in rounds:
        metrics = row["metrics"]
        candidate = f"`{row['candidate_id'][:18]}`" if row["candidate_id"] else "—（未提议）"
        print(f"| {row['round']} | {row['decision'].upper()} | {candidate} | "
              f"{fmt(metrics.get('parent_em'), 'pct')} | {fmt(metrics.get('candidate_em'), 'pct')} | "
              f"{fmt(metrics.get('delta_em'))} | {fmt(metrics.get('delta_f1'))} | "
              f"{fmt(metrics.get('regression_rate'), 'pct')} | {'；'.join(row['reasons']) or '—'} |")


def print_test_phase(result, store):
    test = result["test_metrics"]
    print("\n最终测试集（只在最后运行，不参与选择）：")
    print(f"- 基线：EM {fmt(test['baseline']['em'], 'pct')} / F1 {fmt(test['baseline']['f1'], 'pct')}")
    print(f"- 最终：EM {fmt(test['candidate']['em'], 'pct')} / F1 {fmt(test['candidate']['f1'], 'pct')}"
          f"（{fmt(test['delta_em'])}）")
    print(f"- 选择过程使用测试集：{test['selection_used_test']}；"
          f"测试集策略：{test.get('test_policy')}；协议哈希：{result['protocol_hash'][:16]}")
    statistics = test.get("statistics") or {}
    mde = statistics.get("mde") or {}
    mcnemar = statistics.get("mcnemar") or {}
    interval = statistics.get("bca") or {}
    if mde:
        print(f"- 测试集统计：精确 McNemar p={mcnemar.get('p_value'):.4f}；"
              f"BCa 区间 {interval.get('interval')}；MDE={mde.get('mde_pp'):.1f}pp；"
              f"underpowered={statistics.get('underpowered')}")
    print(f"- 测试集协议守卫：{test.get('guard')}")


def print_receipts(result):
    print(f"\n决策凭证（{len(result['receipts'])} 条）：")
    for receipt in result["receipts"]:
        print(f"- {receipt['decision']:<11} {receipt['receipt_id'][:24]} "
              f"profile={receipt['profile']} hash={receipt['receipt_hash'][:16]} "
              f"protocol={receipt['protocol_hash'][:12]}")
    print(f"- 链校验：verified={result['chain_verified']} count={result['chain_count']}")


def chain_state(store):
    with store.connection() as conn:
        chain = verify_chain(conn)
    return chain["verified"], chain["count"]


# ---------------------------------------------------------------------------
# 两个场景
# ---------------------------------------------------------------------------

def run_scenario_a(root: Path) -> dict:
    print("\n"+"="*78)
    print("场景 A：基本闭环（bad → 拒绝，good → 接受，第 3 轮停止）")
    print("="*78)
    store, engine = build_state(root, TASKS_A, LABELS_A)
    result = engine.evolve("campus-synthetic", [t for t, _, _ in TASKS_A if t.startswith("v")],
                           [t for t, _, _ in TASKS_A if t.startswith("t")],
                           ScriptedProposer(store, ["bad_patch", "good_patch", None]),
                           MockRunner(responder_a, workers=4, unit_cost_cny=0.0002),
                           gate_profile="screen", max_rounds=3, budget_cny=5.0)
    print("6 验证 / 6 测试，门禁档位 screen（两档判定逻辑相同，只有阈值不同）。\n")
    print_table(result["rounds"])
    print_test_phase(result, store)
    result["chain_verified"], result["chain_count"] = chain_state(store)
    print_receipts(result)
    print(f"\n停止原因：{result['stop_reason']}")
    print(f"最终版本：{result['final_version']}（基线 {result['base_version']}）")
    print(f"成本：¥{result['cost_cny']:.4f}（模拟费用；API 调用 {result['api_calls']} 次；"
          f"external_api={result['external_api']}；真实费用 ¥{result['actual_cost_cny']:.4f}）")

    assert len(result["rounds"]) == 3, "场景 A 必须跑满 3 轮"
    assert [row["decision"] for row in result["rounds"]] == ["reject", "accept", "stop"]
    assert result["final_version"] != result["base_version"]
    final_prompt = store.load_artifact(store.version(result["final_version"])["artifacts"]["prompt"])
    assert "先逐跳确定" in final_prompt["components"]["final_answer.predict"]
    assert result["chain_verified"] is True
    assert result["test_metrics"]["delta_em"] > 0
    assert result["test_metrics"]["selection_used_test"] is False
    assert result["external_api"] is False
    return result


def run_scenario_b(root: Path) -> dict:
    print("\n"+"="*78)
    print("场景 B：选择偏差（连续比较 5 个候选，触发族错误率警告）")
    print("="*78)
    store, engine = build_state(root, TASKS_B, LABELS_B)
    result = engine.evolve("campus-synthetic", VAL_B, TEST_B,
                           ScriptedProposer(store, STEPS_B),
                           MockRunner(responder_b, workers=4, unit_cost_cny=0.0002),
                           gate_profile="screen", max_rounds=len(STEPS_B), budget_cny=5.0)
    print("8 验证 / 8 测试；5 个候选依次与当时版本比较（accept / reject 交替）。\n")
    print_table(result["rounds"])

    selection = result["selection"] or {}
    print("\n选择偏差审计：")
    print(f"- 比较次数：{selection['comparisons']}；"
          f"有效比较：{selection['effective_comparisons']}；"
          f"无效比较：{selection['ineffective_comparisons']}")
    print(f"- 不同候选：{selection['distinct_candidates']}；"
          f"不同版本：{selection['distinct_versions']}；"
          f"Bonferroni α：{selection['bonferroni_alpha']:.4f}")
    print(f"- 每个候选被比较次数：{selection['candidate_counts']}")
    print(f"- 族错误率警告：{selection['family_wise_warning']}")
    print(f"- 每轮比较次数：{[row['comparison_count'] for row in result['rounds']]}")

    print_test_phase(result, store)
    result["chain_verified"], result["chain_count"] = chain_state(store)
    print_receipts(result)
    print(f"\n停止原因：{result['stop_reason']}")
    print(f"最终版本：{result['final_version']}（基线 {result['base_version']}）")
    print(f"成本：¥{result['cost_cny']:.4f}（模拟费用；API 调用 {result['api_calls']} 次）")

    trace = TraceLog(store.root/"trace"/"events.jsonl").summary()
    print(f"事件日志：{trace['events']} 条（{len(trace['by_event'])} 种事件）；"
          f"路径 trace/events.jsonl")

    assert len(result["rounds"]) == 5, "场景 B 必须比较 5 个候选"
    assert [row["decision"] for row in result["rounds"]] == \
        ["reject", "accept", "reject", "accept", "reject"]
    assert len([row for row in result["rounds"] if row["candidate_id"]]) == 5
    assert selection["distinct_candidates"] == 5
    assert selection["effective_comparisons"] == 5
    assert selection["family_wise_warning"] is not None, "候选数 > 3 必须输出族错误率警告"
    assert "Bonferroni" in selection["family_wise_warning"]
    assert [row["comparison_count"] for row in result["rounds"]] == [1, 1, 1, 1, 1]
    assert len(result["accepted"]) == 2 and len(result["rejected"]) == 3
    assert result["chain_verified"] is True and result["chain_count"] == 5
    assert all(receipt["protocol_hash"] == result["protocol_hash"] for receipt in result["receipts"])
    assert result["test_metrics"]["selection_used_test"] is False
    assert result["external_api"] is False
    return result, store


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state", type=Path, default=None,
                        help="状态根目录（必须不存在）；会创建 scenario-a / scenario-b")
    parser.add_argument("--report", type=Path, default=None, help="为场景 B 额外写出 Markdown 报告")
    parser.add_argument("--json", action="store_true", help="末尾额外输出一行机器可读摘要")
    args = parser.parse_args(argv)

    began = time.monotonic()
    temp = None
    root = args.state
    if root is None:
        temp = tempfile.TemporaryDirectory(prefix="evolve-demo-")
        root = Path(temp.name)
    elif root.exists():
        print(f"状态目录已存在，演示不覆盖：{root}", file=sys.stderr)
        return 2

    print("在线闭环演示（MockRunner；两个场景；不联网）")
    result_a = run_scenario_a(root/"scenario-a")
    result_b, store_b = run_scenario_b(root/"scenario-b")

    if args.report:
        write_report(store_b, args.report, evolution_run_id=result_b["run_id"])
        print(f"报告：{args.report}")
    elapsed = time.monotonic()-began
    print(f"\n两个场景全部断言通过；耗时 {elapsed:.2f} 秒；状态目录 {root}")
    if args.json:
        print(json.dumps(dict(
            scenario_a=dict(rounds=len(result_a["rounds"]), stop_reason=result_a["stop_reason"],
                            accepted=len(result_a["accepted"]), rejected=len(result_a["rejected"]),
                            test_delta_em=result_a["test_metrics"]["delta_em"]),
            scenario_b=dict(rounds=len(result_b["rounds"]), stop_reason=result_b["stop_reason"],
                            accepted=len(result_b["accepted"]), rejected=len(result_b["rejected"]),
                            effective_comparisons=result_b["selection"]["effective_comparisons"],
                            distinct_candidates=result_b["selection"]["distinct_candidates"],
                            family_wise_warning=result_b["selection"]["family_wise_warning"],
                            receipts=len(result_b["receipts"])),
            elapsed_s=round(elapsed, 3)), ensure_ascii=False))
    if temp is not None:
        temp.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
