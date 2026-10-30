"""独立门禁：从持久化原始输出重算既有EM/F1，不接受调用方提供的均分。"""

import random
import unicodedata

from scripts.scoring import f1_score, normalize
from .contracts import GateSpec, content_hash, now
from . import stats

TOL = 1e-12


def _nfd(text: str) -> str:
    return unicodedata.normalize("NFD", normalize(text))


def exact_match(answer: str, gold: str) -> float:
    """HotpotQA 语义的 EM：规范化（含 Unicode NFD）后完全相等。

    与 DSPy ``answer_exact_match`` 在同样规范化下的结果一致；用本地实现是为了让
    harness/演示/门禁回放不依赖 7 秒级的 DSPy 导入。等价性由
    ``tests/test_scorer_equivalence.py`` 在装有 dspy 时逐例校验。
    """
    pred, ref = _nfd(answer), _nfd(gold)
    if pred in {"yes", "no", "noanswer"} and pred != ref:
        return 0.0
    return 1.0 if pred == ref else 0.0


def score(answer: str, gold: str) -> tuple[float, float]:
    return exact_match(answer, gold), f1_score(answer, gold)


class CandidateValidator:
    def __init__(self, store):
        self.store = store

    # ---- 指标层：只依赖运行记录与标签，不涉及候选身份 ----

    def summarize(self, conn, spec: GateSpec, run: dict) -> dict:
        """单个运行在冻结清单上的重算结果（EM/F1），供基线与测试集汇报使用。"""
        dataset = self.store.get(conn, "datasets", spec.dataset_id)
        by_key = {(r["task_id"], r["repeat"]): r for r in run["outputs"]}
        rows = []
        for task_id in spec.task_ids:
            for repeat in range(1, spec.repeats+1):
                row = by_key[(task_id, repeat)]
                em, f1 = score(row["answer"], dataset["labels"][task_id])
                rows.append(dict(task_id=task_id, repeat=repeat, em=em, f1=f1))
        n = len(rows)
        return dict(count=n, em=sum(r["em"] for r in rows)/n, f1=sum(r["f1"] for r in rows)/n,
                    per_task=rows)

    def compare(self, conn, spec: GateSpec, parent: dict, child: dict) -> dict:
        """父子配对比较：EM/F1/回归率/配对 bootstrap 区间/成本比。

        区间沿用 v1.1 的百分位口径（seed/重采样次数现在由 ``GateSpec`` 冻结），
        另附 BCa、精确 McNemar、Wilson 与 MDE；门禁仍只消费 ``interval``，
        因此历史回放决策不变。
        """
        dataset = self.store.get(conn, "datasets", spec.dataset_id)
        index = [{(r["task_id"], r["repeat"]): r for r in run["outputs"]} for run in [parent, child]]
        rows = []
        for task_id in spec.task_ids:
            for repeat in range(1, spec.repeats+1):
                key = (task_id, repeat)
                old, new = [score(m[key]["answer"], dataset["labels"][task_id]) for m in index]
                rows.append(dict(task_id=task_id, repeat=repeat, parent_em=old[0], candidate_em=new[0],
                                 parent_f1=old[1], candidate_f1=new[1]))
        n = len(rows)
        parent_em = sum(r["parent_em"] for r in rows)/n
        candidate_em = sum(r["candidate_em"] for r in rows)/n
        delta_f1 = sum(r["candidate_f1"]-r["parent_f1"] for r in rows)/n
        correct = sum(r["parent_em"] for r in rows)
        regressed = sum(r["parent_em"] == 1 and r["candidate_em"] == 0 for r in rows)
        fixed = sum(r["parent_em"] == 0 and r["candidate_em"] == 1 for r in rows)
        regression = regressed/correct if correct else None
        deltas = [sum(r["candidate_em"]-r["parent_em"] for r in rows if r["task_id"] == task)/spec.repeats
                  for task in spec.task_ids]
        rng = random.Random(spec.seed)
        samples = sorted(sum(rng.choices(deltas, k=len(deltas)))/len(deltas)
                         for _ in range(spec.bootstrap_resamples))
        lower = max(0, min(spec.bootstrap_resamples-1, int(0.025*spec.bootstrap_resamples)-1))
        upper = max(0, min(spec.bootstrap_resamples-1, int(0.975*spec.bootstrap_resamples)-1))
        interval = [samples[lower], samples[upper]]
        analysis = stats.analyze_paired([r["parent_em"] for r in rows],
                                        [r["candidate_em"] for r in rows],
                                        resamples=spec.bootstrap_resamples, seed=spec.seed,
                                        min_sample=spec.min_sample)
        mode = "real" if "real" in {parent["mode"], child["mode"]} else "simulation"
        return dict(parent_em=parent_em, candidate_em=candidate_em,
            delta_em=candidate_em-parent_em, delta_f1=delta_f1, regression_rate=regression,
            regression_count=regressed, fixed=fixed, regressed=regressed, count=n,
            discordant=fixed+regressed,
            interval=interval, interval_bca=analysis["bca"]["interval"],
            per_task=rows,
            mcnemar=analysis["mcnemar"], p_value=analysis["mcnemar"]["p_value"],
            wilson_parent=analysis["wilson_parent"], wilson_candidate=analysis["wilson_candidate"],
            mde=analysis["mde"], underpowered=analysis["underpowered"],
            delta_significant=analysis["delta_significant"], statistics=analysis,
            parent_cost_cny=parent["projected_cost_cny"], candidate_cost_cny=child["projected_cost_cny"],
            parent_api_calls=parent.get("api_calls", 0), candidate_api_calls=child.get("api_calls", 0),
            cost_kind="actual_api_cost" if mode == "real" else "simulation_projection")

    # ---- 门禁层 ----

    def analyze(self, conn, candidate: dict, parent_run_id: str, candidate_run_id: str) -> dict:
        spec = GateSpec.from_dict(candidate["validation_config"])
        base = dict(candidate_id=candidate["candidate_id"], mode="simulation", created_at=now(),
            run_ids=[parent_run_id, candidate_run_id], task_ids=list(spec.task_ids),
            condition_hash=content_hash(spec.to_dict()), metrics={}, reasons=[], passed=False)
        parent = self.store.get(conn, "runs", parent_run_id)
        child = self.store.get(conn, "runs", candidate_run_id)
        reasons = base["reasons"]
        base["mode"] = "real" if "real" in {parent["mode"], child["mode"]} else "simulation"
        if parent["version_id"] != candidate["parent_version_id"] or child["version_id"] != candidate["version_id"]:
            reasons.append("父/候选运行版本不匹配")
        if child["candidate_id"] != candidate["candidate_id"]:
            reasons.append("候选运行身份不匹配")
        if self.store.current_id(conn) != candidate["parent_version_id"]:
            reasons.append("当前版本已改变，候选父版本过期")
        expected = {(task_id, repeat) for task_id in spec.task_ids for repeat in range(1, spec.repeats+1)}
        for run in [parent, child]:
            if run["status"] != "completed":
                reasons.append("评价运行失败或未完整完成")
            if run["condition_hash"] != base["condition_hash"] or run["validation_config"] != spec.to_dict():
                reasons.append("评价条件与预注册配置不同")
            if spec.require_offline and (run["mode"] != "simulation" or run["api_calls"] != 0
                                         or run["actual_api_cost_cny"] != 0):
                reasons.append("离线模式/真实调用边界不符")
            if run["mode"] not in {"simulation", "real"}:
                reasons.append("未知执行模式")
            keys = [(r["task_id"], r["repeat"]) for r in run["outputs"]]
            if len(keys) != len(expected) or set(keys) != expected:
                reasons.append("逐题预测缺失、重复或样本清单改变")
            if any(r["status"] != "completed" for r in run["outputs"]):
                reasons.append("执行中存在错误")
            attempts = [self.store.get(conn, "attempts", r["attempt_id"]) for r in run["outputs"]]
            if any(a["status"] != "settled" or a["run_id"] != run["run_id"] or
                   a["version_id"] != run["version_id"] or a["scope_id"] != run["scope_id"] for a in attempts):
                reasons.append("运行费用与结算槽位不一致")
            accounted = sum(a["projected_cost_cny"] or 0 for a in attempts)
            if abs(accounted-run["projected_cost_cny"]) > 1e-10:
                reasons.append("运行费用与账本不一致")
            if run["mode"] == "real":
                actual = sum(a.get("actual_api_cost_cny", 0) or 0 for a in attempts)
                calls = sum(a.get("api_calls", 0) or 0 for a in attempts)
                if abs(actual-run["actual_api_cost_cny"]) > 1e-10 or calls != run["api_calls"]:
                    reasons.append("真实调用账本与运行记录不一致")
            scope = self.store.get(conn, "budgets", run["scope_id"])
            if scope["status"] != "open" or scope["reserved_cny"] > 1e-12 or scope["accounted_cny"] > scope["limit_cny"]+1e-12:
                reasons.append("预算已终止、超限或有未决预留")
            if scope["accounted_cny"] > spec.max_total_cost_cny+1e-12:
                reasons.append("预算作用域累计成本超过冻结验证上限")
        if parent["scope_id"] != child["scope_id"]:
            reasons.append("父子未使用相同预算作用域")
        if parent["mode"] != child["mode"]:
            reasons.append("父子执行模式不同")
        if parent["projected_cost_cny"]+child["projected_cost_cny"] > spec.max_total_cost_cny+1e-12:
            reasons.append("验证总成本超过冻结上限")
        if reasons:
            base["verdict"] = "fail"
            base["evidence"] = dict(count=len(spec.task_ids)*spec.repeats,
                                    structural_failure=True,
                                    notes=["存在结构性证据问题，未进入统计判定"])
            return base
        metrics = self.compare(conn, spec, parent, child)
        base["metrics"] = metrics
        # 门禁判据集中在 harness.gates 的纯函数里，验证器只负责重算指标。
        from .gates import assess_gate, cost_ratio
        base["metrics"]["cost_ratio"] = cost_ratio(base["metrics"])
        assessment = assess_gate(base["metrics"], spec)
        reasons.extend(assessment["reasons"])
        base["passed"] = assessment["passed"]
        base["verdict"] = assessment["verdict"]
        base["evidence"] = assessment["evidence"]
        base["statistics"] = metrics.get("statistics")
        base["count"] = metrics.get("count")
        return base
