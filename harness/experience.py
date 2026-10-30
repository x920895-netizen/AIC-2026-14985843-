"""证据引用、经验生命周期和可检索状态；模拟验证不升级为真实证据。"""

from dataclasses import asdict

from .contracts import EvidenceRef, new_id, now
from .storage import StateConflict
from .evaluation import score


class ExperienceManager:
    STATES = {"pending", "verified", "rejected", "conflict", "expired", "retired"}

    def __init__(self, store):
        self.store = store

    def check_sources(self, conn, refs: list[dict]):
        if not refs:
            raise ValueError("经验必须有实际运行输出引用")
        for ref in refs:
            try:
                run = self.store.get(conn, "runs", ref["run_id"])
            except KeyError as exc:
                raise ValueError("证据引用的运行不存在") from exc
            if not any(row["task_id"] == ref["task_id"] and row["repeat"] == ref["repeat"] for row in run["outputs"]):
                raise ValueError("证据引用无法定位实际任务/重复输出")

    def create(self, content: str, sources: list[EvidenceRef], applicable_if: dict,
               *, experience_id: str | None = None) -> dict:
        if not content or not isinstance(applicable_if, dict) or not applicable_if:
            raise ValueError("经验须包含内容和明确适用条件")
        refs = [asdict(r) for r in sources]
        timestamp = now()
        exp = dict(experience_id=experience_id or new_id("experience"), content=content,
            source_refs=refs, support_evidence=refs, counter_evidence=[], evaluation_results=[],
            applicable_if=applicable_if, status="pending", verification_level="unverified",
            validation_results=[], candidate_ids=[], retirement_reason=None, created_at=timestamp, updated_at=timestamp)
        with self.store.transaction("experience.create") as conn:
            self.check_sources(conn, refs)
            for ref in refs:
                run = self.store.get(conn, "runs", ref["run_id"])
                row = next(r for r in run["outputs"] if r["task_id"] == ref["task_id"] and r["repeat"] == ref["repeat"])
                labels = self.store.get(conn, "datasets", run["validation_config"]["dataset_id"])["labels"]
                em, f1 = score(row["answer"], labels[ref["task_id"]])
                exp["evaluation_results"].append(dict(run_id=ref["run_id"], task_id=ref["task_id"],
                    repeat=ref["repeat"], em=em, f1=f1, mode=run["mode"],
                    raw_output_ref=f"runs/{run['run_id']}#outputs/{ref['task_id']}/{ref['repeat']}"))
            self.store.put(conn, "experiences", exp["experience_id"], exp, insert=True)
            self.store.event(conn, "experience_created", dict(experience_id=exp["experience_id"]))
        return exp

    def verify(self, experience_id: str, evaluation_id: str, resolution: str = "") -> dict:
        with self.store.transaction("experience.verify") as conn:
            exp = self.store.get(conn, "experiences", experience_id)
            report = self.store.get(conn, "evaluations", evaluation_id)
            candidate = self.store.get(conn, "candidates", report["candidate_id"])
            if experience_id not in candidate["experience_ids"]:
                raise StateConflict("验证报告没有绑定该经验")
            if not report["passed"] or len(report["task_ids"]) < 2:
                raise StateConflict("经验验证须有通过门禁的多任务报告")
            if report["created_at"] <= exp["updated_at"]:
                raise StateConflict("必须使用经验最近更新之后的新验证报告")
            if exp["status"] in {"conflict", "expired", "retired", "rejected"}:
                if not resolution or report["created_at"] <= exp["updated_at"]:
                    raise StateConflict("有冲突/已失效经验须有新的验证及解决说明")
            exp.update(status="verified", verification_level="offline_simulated", updated_at=now())
            exp["validation_results"].append(dict(evaluation_id=evaluation_id, mode=report["mode"], resolution=resolution))
            self.store.put(conn, "experiences", experience_id, exp)
            self.store.event(conn, "experience_verified", dict(experience_id=experience_id, evaluation_id=evaluation_id))
        return exp

    def transition(self, experience_id: str, status: str, reason: str,
                   counter_evidence: list[EvidenceRef] | None = None) -> dict:
        if status not in self.STATES or status == "verified" or not reason:
            raise ValueError("状态无效；已验证状态必须经verify和真实报告引用取得")
        refs = [asdict(r) for r in counter_evidence or []]
        if status == "conflict" and not refs:
            raise ValueError("冲突状态必须有反例引用")
        with self.store.transaction("experience.transition") as conn:
            exp = self.store.get(conn, "experiences", experience_id)
            if refs:
                self.check_sources(conn, refs)
            exp["counter_evidence"].extend(refs)
            old = exp["status"]
            exp.update(status=status, updated_at=now(), retirement_reason=reason)
            self.store.put(conn, "experiences", experience_id, exp)
            self.store.event(conn, "experience_transition", dict(experience_id=experience_id, old=old, new=status, reason=reason))
        return exp

    def search(self, query: str, *, statuses=("verified",), applicable_if: dict | None = None,
               allow_simulated=False, active_only=False) -> list[dict]:
        if not set(statuses) <= self.STATES:
            raise ValueError("经验状态过滤无效")
        active = None
        if active_only:
            version = self.store.current()
            active = set(self.store.load_artifact(version["artifacts"]["memory"])["experience_ids"])
        tokens = set(query.casefold().split())
        results = []
        for exp in self.store.list("experiences"):
            if exp["status"] not in statuses or (active is not None and exp["experience_id"] not in active):
                continue
            if exp["status"] == "verified" and not allow_simulated and exp["verification_level"] != "real":
                continue
            if applicable_if and any(exp["applicable_if"].get(k) != v for k, v in applicable_if.items()):
                continue
            score = sum(word in exp["content"].casefold() for word in tokens)
            if not tokens or score:
                results.append(dict(exp, retrieval_score=score))
        return sorted(results, key=lambda r: (-r["retrieval_score"], r["experience_id"]))
