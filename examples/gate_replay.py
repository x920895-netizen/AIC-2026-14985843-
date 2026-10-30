"""两级门禁历史回放：用 R1/R2 的真实配对计数复算决策。

不读取原始问答文本，只读取 ``evidence/historical_r1.json`` /
``evidence/historical_r2.json`` 里的计数、指标与成本，然后用与
``harness.evaluation`` 相同的配对 bootstrap（seed=1，2000 次，百分位
索引 49/1949）重算区间，再交给 ``harness.gates.evaluate_gate`` 判定。

用法::

    python examples/gate_replay.py                 # 输出 ./gate_replay.json
    python examples/gate_replay.py --output evidence/gate_replay.json
"""

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import GateSpec  # noqa: E402
from harness.gates import evaluate_gate, RELEASE_THRESHOLDS, SCREEN_THRESHOLDS  # noqa: E402

EVIDENCE = ROOT / "evidence"

BOOTSTRAP_SEED = 1
BOOTSTRAP_RESAMPLES = 2000
LOWER_INDEX = 49
UPPER_INDEX = 1949
TOL = 1e-9

#: 事实表锁定的期望决策；脚本与测试都以这里为准。
EXPECTED = {
    "R1": {
        "screen": (True, []),
        "release": (False, ["候选推理成本比超限"]),
    },
    "R2": {
        "screen": (False, ["主指标EM增益不足"]),
        "release": (False, ["主指标EM增益不足", "辅助指标F1退化超限",
                            "逐题回归率超限", "配对区间不足以支持正增益"]),
    },
}


def load_experiment(name: str) -> dict:
    path = EVIDENCE / f"historical_{name.lower()}.json"
    if not path.is_file():
        raise FileNotFoundError(f"缺少证据文件：{path.relative_to(ROOT)}（请从仓库根目录运行）")
    return json.loads(path.read_text(encoding="utf-8"))


def paired_deltas(experiment: dict) -> list[float]:
    """把配对计数还原为逐题 EM 差值：修复=+1，回归=-1，其余=0。"""
    paired, n = experiment["paired"], experiment["dataset"]["n"]
    fixed, regressed = paired["fixed"], paired["regressed"]
    if fixed + regressed > n:
        raise ValueError("修复/回归计数超过题数")
    if paired["candidate_correct"] != paired["baseline_correct"] + fixed - regressed:
        raise ValueError("配对计数与候选正确数不自洽")
    deltas = [1.0] * fixed + [-1.0] * regressed
    deltas += [0.0] * (n - len(deltas))
    return deltas


def bootstrap_interval(deltas: list[float], *, seed: int = BOOTSTRAP_SEED,
                       resamples: int = BOOTSTRAP_RESAMPLES) -> list[float]:
    """与 harness.evaluation.CandidateValidator 完全相同的重采样方式。"""
    rng = random.Random(seed)
    samples = sorted(sum(rng.choices(deltas, k=len(deltas))) / len(deltas) for _ in range(resamples))
    return [samples[LOWER_INDEX], samples[UPPER_INDEX]]


def metrics_from(experiment: dict) -> dict:
    paired, n = experiment["paired"], experiment["dataset"]["n"]
    baseline_em = paired["baseline_correct"] / n
    candidate_em = paired["candidate_correct"] / n
    stored = experiment["metrics"]
    if abs(stored["baseline_em"] - baseline_em) > TOL or abs(stored["candidate_em"] - candidate_em) > TOL:
        raise AssertionError("计数与存档 EM 不一致，拒绝回放")
    interval = bootstrap_interval(paired_deltas(experiment))
    expected_interval = experiment["statistics"]["bootstrap"]
    if abs(interval[0] - expected_interval["lower"]) > TOL or abs(interval[1] - expected_interval["upper"]) > TOL:
        raise AssertionError(f"bootstrap 区间与存档不一致：{interval} vs "
                             f"[{expected_interval['lower']}, {expected_interval['upper']}]")
    regression = (paired["regressed"] / paired["baseline_correct"]) if paired["baseline_correct"] else None
    cost = experiment["cost"]
    ratio = cost["ratio"]
    if cost["kind"] == "api_cost_cny_from_test_phase":
        computed = cost["candidate_cny"] / cost["baseline_cny"]
        parent_cost, candidate_cost = cost["baseline_cny"], cost["candidate_cny"]
    elif cost["kind"] == "per_task_latency_proxy":
        computed = cost["candidate_seconds_per_task"] / cost["baseline_seconds_per_task"]
        parent_cost, candidate_cost = (cost["baseline_seconds_per_task"],
                                       cost["candidate_seconds_per_task"])
    else:
        raise ValueError(f"未知成本口径：{cost['kind']}")
    if abs(computed - ratio) > TOL:
        raise AssertionError("成本比与存档不一致，拒绝回放")
    return dict(
        parent_em=baseline_em,
        candidate_em=candidate_em,
        delta_em=candidate_em - baseline_em,
        delta_f1=stored["candidate_f1"] - stored["baseline_f1"],
        regression_rate=regression,
        regression_count=paired["regressed"],
        interval=interval,
        parent_cost_cny=parent_cost,
        candidate_cost_cny=candidate_cost,
        cost_ratio=ratio,
        cost_kind=cost["kind"],
        per_task=[],
    )


def gate_specs(experiment: dict) -> dict:
    n = experiment["dataset"]["n"]
    task_ids = tuple(f"q{i:02d}" for i in range(n))
    model = {"model": experiment["dataset"]["model"], "temperature": 0}
    return {
        "screen": GateSpec.screen(f"{experiment['experiment'].lower()}-replay", task_ids, model),
        "release": GateSpec.release(f"{experiment['experiment'].lower()}-replay", task_ids, model),
    }


def replay(experiment: dict) -> dict:
    metrics = metrics_from(experiment)
    specs = gate_specs(experiment)
    decisions = {}
    for profile, spec in specs.items():
        passed, reasons = evaluate_gate(metrics, spec)
        decisions[profile] = dict(passed=passed, reasons=reasons,
                                  max_em_gain=spec.min_em_gain, max_f1_drop=spec.max_f1_drop,
                                  max_regression_rate=spec.max_regression_rate,
                                  max_cost_ratio=spec.max_cost_ratio,
                                  require_positive_interval=spec.require_positive_interval)
    return dict(metrics=metrics, decisions=decisions)


def check_expectations(name: str, result: dict) -> None:
    for profile, (passed, reasons) in EXPECTED[name].items():
        actual = result["decisions"][profile]
        if actual["passed"] is not passed or actual["reasons"] != reasons:
            raise AssertionError(f"{name} {profile} 决策与事实表不符："
                                 f"{actual['passed']} {actual['reasons']} != {passed} {reasons}")


def format_table(results: dict) -> str:
    lines = ["| 实验 | 档位 | 决策 | EM 变化 | F1 变化 | 回归率 | 成本比 | 配对区间 | 否决理由 |",
             "| --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |"]
    for name, result in results.items():
        m = result["metrics"]
        for profile in ("screen", "release"):
            d = result["decisions"][profile]
            reasons = "；".join(d["reasons"]) or "—"
            reg = "n/a" if m["regression_rate"] is None else f"{m['regression_rate']*100:.2f}%"
            lines.append(f"| {name} | {profile} | {'PASS' if d['passed'] else 'FAIL'} | "
                         f"{m['delta_em']*100:+.2f}pp | {m['delta_f1']*100:+.2f}pp | {reg} | "
                         f"{m['cost_ratio']:.4f} | [{m['interval'][0]*100:+.2f}, {m['interval'][1]*100:+.2f}] | "
                         f"{reasons} |")
    return "\n".join(lines)


def build_report(results: dict) -> dict:
    methods = dict(
        bootstrap_seed=BOOTSTRAP_SEED, resamples=BOOTSTRAP_RESAMPLES,
        interval_indices=[LOWER_INDEX, UPPER_INDEX],
        bootstrap="逐题 EM 差值的配对重采样百分位区间（与 harness.evaluation 同法）",
        screen_thresholds={k: SCREEN_THRESHOLDS[k] for k in
                           ("min_em_gain", "max_f1_drop", "max_regression_rate", "max_cost_ratio",
                            "require_positive_interval")},
        release_thresholds={k: RELEASE_THRESHOLDS[k] for k in
                            ("min_em_gain", "max_f1_drop", "max_regression_rate", "max_cost_ratio",
                             "require_positive_interval")},
        source=["evidence/historical_r1.json", "evidence/historical_r2.json"],
    )
    return dict(schema="gate-replay/1", generated_by="examples/gate_replay.py",
                methods=methods, experiments=results,
                note="R2 的成本口径为每题平均延迟代理，不是账单费用；R1 为测试阶段 API 费用估算。")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=Path("gate_replay.json"),
                        help="输出 JSON 路径（默认当前目录）")
    args = parser.parse_args(argv)
    try:
        experiments = {name: load_experiment(name) for name in ("R1", "R2")}
        results = {name: replay(exp) for name, exp in experiments.items()}
        for name, result in results.items():
            check_expectations(name, result)
    except (AssertionError, FileNotFoundError, ValueError) as exc:
        print(f"回放失败：{exc}", file=sys.stderr)
        return 1
    report = build_report(results)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(format_table(results))
    print(f"\n已写出：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
