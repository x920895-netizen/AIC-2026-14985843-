"""公共候选生命周期协调器；四类策略通过同一门禁和事务发布。

v1.2 在 v1.1 的在线闭环之上补充：

- ``accept`` 幂等：重复接受同一候选返回既有凭证，不再抛“状态已变”；
- ``evolve`` 分阶段持久化（提议 / 运行 / 验证 / 决策），任一步中断都可续跑；
- 每轮记录 ``verdict``（pass / fail / insufficient）与选择偏差摘要；
- 测试集通过协议守卫只跑一次，测试阶段本身也支持断点续跑；
- 事件写入 JSONL trace，便于事后按 run / candidate 回放。
"""

import difflib
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .budget import BudgetLedger
from .contracts import (CandidateProposal, GateSpec, RuntimeTask, TargetKind, content_hash,
                        new_id, nonnegative, now)
from .evaluation import CandidateValidator, score
from .execution import OfflineExecutor, OnlineExecutor
from .gates import gate_profile
from .experience import ExperienceManager
from .protocol import ProtocolSpec, TestSetGuard, build_protocol, protocol_warnings
from .proposers.base import FailureCase, ProposalContext
from .receipt import ReceiptLedger
from .selection import build_summary
from .storage import StateConflict
from .trace import TraceLog

EVOLUTION = "evolution_runs"
#: 阶段持久化：这些名字同时是断点注入点。
STEPS = ("propose", "run", "validate", "decide")


def _elapsed_since(started_at: str | None) -> float:
    if not started_at:
        return 0.0
    try:
        began = datetime.fromisoformat(started_at)
        if began.tzinfo is None:
            began = began.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - began).total_seconds())
    except (TypeError, ValueError):
        return 0.0


class EvolutionFoundation:
    def __init__(self, store, *, executor=None, trace=None):
        self.store = store
        self.budget = BudgetLedger(store)
        self.experiences = ExperienceManager(store)
        self.executor = executor or OfflineExecutor(store)
        self.validator = CandidateValidator(store)
        self.receipts = ReceiptLedger(store)
        if trace is False:
            self.trace = TraceLog(None)
        elif trace is None:
            self.trace = TraceLog(store.root/"trace"/"events.jsonl")
        elif isinstance(trace, (str, Path)):
            self.trace = TraceLog(trace)
        else:
            self.trace = trace

    # ------------------------------------------------------------------
    # 基础生命周期
    # ------------------------------------------------------------------

    def register_dataset(self, dataset_id: str, tasks: list[RuntimeTask], labels: dict[str, str]) -> dict:
        ids = [t.task_id for t in tasks]
        if not dataset_id or not ids or len(set(ids)) != len(ids) or set(ids) != set(labels):
            raise ValueError("离线任务与评分标签必须完整、唯一且对应")
        if any(not isinstance(v, str) for v in labels.values()):
            raise ValueError("评分标签须为字符串")
        dataset = dict(dataset_id=dataset_id, mode="simulation", tasks=[asdict(t) for t in tasks],
                       labels=labels, created_at=now())
        with self.store.transaction("dataset.register") as conn:
            self.store.put(conn, "datasets", dataset_id, dataset, insert=True)
        return dataset

    def register(self, proposal: CandidateProposal, *, protocol: ProtocolSpec | None = None) -> dict:
        target = TargetKind(proposal.target).value
        if not proposal.basis:
            raise ValueError("候选须有变更依据")
        gate = proposal.gate.to_dict()
        with self.store.transaction("candidate.register") as conn:
            parent = self.store.get_version(conn, proposal.parent_version_id)
            if self.store.current_id(conn) != proposal.parent_version_id or parent["status"] != "accepted":
                raise StateConflict("候选必须基于当前已接受版本")
            if parent["config_snapshot"]["model_config"] != gate["model_config"]:
                raise StateConflict("候选不能改变基础模型配置")
            dataset = self.store.get(conn, "datasets", gate["dataset_id"])
            dataset_ids = [t["task_id"] for t in dataset["tasks"]]
            gate_ids = list(gate["task_ids"])
            # 允许验证集是数据集的子集（val/test 同库），但顺序必须与数据集一致。
            if not set(gate_ids) <= set(dataset_ids) or [x for x in dataset_ids if x in set(gate_ids)] != gate_ids:
                raise StateConflict("验证数据与冻结清单不一致")
            if proposal.artifact_ref["kind"] != target:
                raise ValueError("目标类型与工件类型不一致")
            old = self.store.load_artifact(parent["artifacts"][target])
            new = self.store.load_artifact(proposal.artifact_ref)
            if old == new:
                raise ValueError("候选工件没有实际变更")
            signature = content_hash(dict(parent=parent["version_id"], target=target,
                artifact_hash=proposal.artifact_ref["content_hash"], gate=gate))
            keys = conn.execute("SELECT id FROM documents WHERE collection='candidates'").fetchall()
            if any(self.store.get(conn, "candidates", key[0])["signature"] == signature for key in keys):
                raise StateConflict("相同父版本、补丁与验证条件已登记，禁止重复付费尝试")
            refs = dict(parent["artifacts"], **{target: proposal.artifact_ref})
            version = self.store.seal_version(dict(version_id=new_id("version"), parent_version_id=parent["version_id"],
                created_at=now(), status="candidate", artifacts=refs, config_snapshot=parent["config_snapshot"],
                evaluator_id=parent["evaluator_id"], experiment_config=dict(parent["experiment_config"], validation=gate),
                decision_ids=[]))
            before = json.dumps(old, ensure_ascii=False, sort_keys=True, indent=2).splitlines(keepends=True)
            after = json.dumps(new, ensure_ascii=False, sort_keys=True, indent=2).splitlines(keepends=True)
            candidate = dict(candidate_id=proposal.candidate_id, parent_version_id=parent["version_id"],
                version_id=version["version_id"], target=target, signature=signature, status="registered",
                change=dict(before=parent["artifacts"][target], after=proposal.artifact_ref,
                            diff="".join(difflib.unified_diff(before, after, fromfile="parent", tofile="candidate"))),
                basis=proposal.basis, experience_ids=list(proposal.experience_ids), expected_benefit=proposal.expected_benefit,
                risks=list(proposal.risks), validation_config=gate, evaluation_ids=[], decision_ids=[],
                comparison_count=0, protocol=(protocol.to_dict() if protocol else None),
                protocol_hash=(protocol.hash if protocol else ""), created_at=now())
            for exp_id in proposal.experience_ids:
                exp = self.store.get(conn, "experiences", exp_id)
                exp["candidate_ids"].append(proposal.candidate_id)
                exp["updated_at"] = now()
                self.store.put(conn, "experiences", exp_id, exp)
            self.store.put(conn, "versions", version["version_id"], version, insert=True)
            self.store.put(conn, "candidates", proposal.candidate_id, candidate, insert=True)
            self.store.event(conn, "candidate_registered", dict(candidate_id=proposal.candidate_id, target=target))
        self.trace.emit("candidate_registered", candidate_id=proposal.candidate_id,
                        version_id=candidate["version_id"], target=target,
                        protocol_hash=candidate["protocol_hash"] or None)
        return candidate

    def run(self, version_id: str, spec: GateSpec, scope_id: str, runner, *, candidate_id=None,
            projected_cost_per_task=0, executor=None, resume_run_id=None, run_id=None) -> dict:
        dataset = self.store.read("datasets", spec.dataset_id)
        by_id = {t["task_id"]: t for t in dataset["tasks"]}
        missing = [task_id for task_id in spec.task_ids if task_id not in by_id]
        if missing:
            raise KeyError(f"冻结清单中的任务不在数据集里：{missing[:3]}")
        tasks = [RuntimeTask(**by_id[task_id]) for task_id in spec.task_ids]
        target = executor or self.executor
        return target.run(tasks, version_id, spec, scope_id, runner,
            candidate_id=candidate_id, projected_cost_per_task=projected_cost_per_task,
            resume_run_id=resume_run_id, run_id=run_id)

    def validate(self, candidate_id: str, parent_run_id: str, child_run_id: str) -> dict:
        began = time.monotonic()
        with self.store.transaction("validation") as conn:
            candidate = self.store.get(conn, "candidates", candidate_id)
            if candidate["status"] != "registered":
                raise StateConflict("候选已评价或终结，不能修改预注册条件后重评")
            try:
                report = self.validator.analyze(conn, candidate, parent_run_id, child_run_id)
            except Exception as exc:
                report = dict(candidate_id=candidate_id, mode="simulation", passed=False, verdict="fail",
                    created_at=now(), run_ids=[parent_run_id, child_run_id],
                    task_ids=candidate["validation_config"]["task_ids"],
                    metrics={}, evidence={"notes": [f"评价失败：{type(exc).__name__}: {exc}"]},
                    reasons=[f"评价失败：{type(exc).__name__}: {exc}"])
            report["evaluation_id"] = new_id("evaluation")
            report["version_hashes"] = [self.store.get_version(conn, v)["content_hash"]
                                       for v in [candidate["parent_version_id"], candidate["version_id"]]]
            spec = GateSpec.from_dict(candidate["validation_config"])
            report["protocol_hash"] = (candidate.get("protocol_hash")
                                       or ProtocolSpec.from_gate_spec(spec).hash)
            report.setdefault("count", len(spec.task_ids)*spec.repeats)
            verdict = report.get("verdict") or ("pass" if report["passed"] else "fail")
            report["verdict"] = verdict
            decision_verdict = ("eligible" if verdict == "pass"
                                else "insufficient" if verdict == "insufficient" else "rejected")
            decision = dict(decision_id=new_id("decision"), candidate_id=candidate_id,
                evaluation_id=report["evaluation_id"], verdict=decision_verdict,
                reasons=report["reasons"] or ["满足冻结门禁，等待原子发布"],
                mode=report.get("mode", "simulation"), verdict_detail=verdict, created_at=now())
            candidate["evaluation_ids"].append(report["evaluation_id"])
            candidate["decision_ids"].append(decision["decision_id"])
            candidate["status"] = "validated" if report["passed"] else "rejected"
            candidate["verdict"] = verdict
            candidate["comparison_count"] = int(candidate.get("comparison_count") or 0) + 1
            candidate["underpowered"] = bool((report.get("evidence") or {}).get("underpowered"))
            if not report["passed"]:
                version = self.store.get_version(conn, candidate["version_id"])
                version.update(status="rejected", decision_ids=candidate["decision_ids"])
                self.store.put(conn, "versions", version["version_id"], self.store.seal_version(version))
            self.store.put(conn, "evaluations", report["evaluation_id"], report, insert=True)
            self.store.put(conn, "decisions", decision["decision_id"], decision, insert=True)
            self.store.put(conn, "candidates", candidate_id, candidate)
            if not report["passed"]:
                # 门禁否决在验证事务内直接落凭证，避免“拒绝但没有记录”。
                self.receipts.record(conn, "insufficient" if verdict == "insufficient" else "reject",
                                     candidate, report)
            self.store.event(conn, "candidate_evaluated", dict(candidate_id=candidate_id,
                                                              passed=report["passed"], verdict=verdict))
        self.trace.emit("candidate_validated", candidate_id=candidate_id, run_id=child_run_id,
                        duration_ms=(time.monotonic()-began)*1000, verdict=verdict,
                        passed=bool(report["passed"]), reasons=list(report["reasons"]),
                        delta_em=(report.get("metrics") or {}).get("delta_em"),
                        p_value=(report.get("statistics") or {}).get("mcnemar", {}).get("p_value"))
        return report

    def accept(self, candidate_id: str) -> dict:
        """接受候选；重复接受幂等返回既有版本与凭证。"""
        began = time.monotonic()
        idempotent = False
        with self.store.transaction("accept") as conn:
            candidate = self.store.get(conn, "candidates", candidate_id)
            if candidate["status"] == "accepted":
                version = self.store.get_version(conn, candidate["version_id"])
                receipt = self.receipts.find(conn, candidate_id, "accept")
                if receipt is None:
                    raise StateConflict("候选已接受但缺少 accept 凭证，状态不可信")
                result = dict(version, receipt=receipt, idempotent=True)
                idempotent = True
            else:
                if candidate["status"] != "validated":
                    raise StateConflict("候选未通过完整门禁，不能接受")
                report = self.store.get(conn, "evaluations", candidate["evaluation_ids"][-1])
                parent = self.store.get_version(conn, candidate["parent_version_id"])
                version = self.store.get_version(conn, candidate["version_id"])
                if [parent["content_hash"], version["content_hash"]] != report["version_hashes"]:
                    raise StateConflict("验证后版本内容发生变化")
                fresh = self.validator.analyze(conn, candidate, *report["run_ids"])
                if not report["passed"] or not fresh["passed"] or fresh["metrics"] != report["metrics"]:
                    raise StateConflict("发布前验证或预算已失效")
                self.store.check_memory(conn, version, allow_simulated=True)
                version.update(status="accepted", decision_ids=candidate["decision_ids"])
                version = self.store.seal_version(version)
                self.store.put(conn, "versions", version["version_id"], version)
                self.store.checkpoint("accept.after_version")
                decision = self.store.get(conn, "decisions", candidate["decision_ids"][-1])
                decision.update(verdict="accepted", reasons=["完整验证、工件校验与预算复核通过"], published_at=now())
                self.store.put(conn, "decisions", decision["decision_id"], decision)
                candidate["status"] = "accepted"
                self.store.put(conn, "candidates", candidate_id, candidate)
                conn.execute("UPDATE meta SET value=? WHERE key='current_version'", (version["version_id"],))
                receipt = self.receipts.record(conn, "accept", candidate, report)
                self.store.event(conn, "accepted", dict(candidate_id=candidate_id, version_id=version["version_id"]))
                self.store.checkpoint("accept.after_pointer")
                result = dict(version, receipt=receipt, idempotent=False)
        self.trace.emit("candidate_accepted", candidate_id=candidate_id,
                        run_id=result["version_id"], duration_ms=(time.monotonic()-began)*1000,
                        idempotent=idempotent)
        return result

    def reject(self, candidate_id: str, reason: str) -> dict:
        """人工/策略显式拒绝：不发布，但必须留下决策与凭证。"""
        if not reason:
            raise ValueError("拒绝必须记录原因")
        began = time.monotonic()
        with self.store.transaction("reject") as conn:
            candidate = self.store.get(conn, "candidates", candidate_id)
            if candidate["status"] not in {"registered", "validated"}:
                raise StateConflict("只能拒绝尚未发布的候选")
            report = (self.store.get(conn, "evaluations", candidate["evaluation_ids"][-1])
                      if candidate["evaluation_ids"] else {})
            decision = dict(decision_id=new_id("decision"), candidate_id=candidate_id,
                evaluation_id=candidate["evaluation_ids"][-1] if candidate["evaluation_ids"] else None,
                verdict="rejected", reasons=[reason], mode="simulation", created_at=now())
            candidate["decision_ids"].append(decision["decision_id"])
            candidate["status"] = "rejected"
            version = self.store.get_version(conn, candidate["version_id"])
            if version["status"] != "accepted":
                version.update(status="rejected", decision_ids=candidate["decision_ids"])
                self.store.put(conn, "versions", version["version_id"], self.store.seal_version(version))
            self.store.put(conn, "decisions", decision["decision_id"], decision, insert=True)
            self.store.put(conn, "candidates", candidate_id, candidate)
            receipt = self.receipts.record(conn, "reject", candidate, report)
            self.store.event(conn, "candidate_rejected", dict(candidate_id=candidate_id, reason=reason))
        self.trace.emit("candidate_rejected", candidate_id=candidate_id,
                        duration_ms=(time.monotonic()-began)*1000, reason=reason)
        return dict(decision=decision, receipt=receipt)

    # ------------------------------------------------------------------
    # 在线自进化闭环
    # ------------------------------------------------------------------

    def evolve(self, dataset_id: str, val_task_ids, test_task_ids, proposer, runner,
               gate_profile: str = "release", max_rounds: int = 3, budget_cny: float = 5.0,
               *, workers: int = 4, record_raw: bool = True, protocol: ProtocolSpec | None = None,
               rate_limit=None) -> dict:
        """跑一个完整的自进化闭环：基线 → 逐轮提议/验证/判定 → 测试集汇报。

        验证集参与选择，测试集只在最后跑一次、不参与任何选择。协议（划分、
        评价器、阈值、重复、种子）一旦给出就冻结；测试阶段和每一轮都可以
        通过 ``resume(run_id, proposer, runner)`` 断点续跑。
        """
        if gate_profile not in {"screen", "release"}:
            raise ValueError("gate_profile 只能是 screen 或 release")
        if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or max_rounds < 1:
            raise ValueError("max_rounds 必须为正整数")
        budget = nonnegative(budget_cny, "预算")
        dataset = self.store.read("datasets", dataset_id)
        val_ids, test_ids = self._check_split(dataset, tuple(val_task_ids), tuple(test_task_ids))
        if protocol is None:
            protocol = build_protocol(dataset_id, val_ids, test_ids, gate_profile=gate_profile)
        elif (protocol.dataset_id != dataset_id or tuple(protocol.val_task_ids) != val_ids
              or tuple(protocol.test_task_ids) != test_ids):
            raise ValueError("协议划分与 evolve 参数不一致")
        elif protocol.gate_profile not in (gate_profile, "custom"):
            raise ValueError("协议门禁档位与 evolve 参数不一致")
        version = self.store.current()
        model_config = version["config_snapshot"]["model_config"]
        spec = protocol.gate_spec(model_config, budget=budget, require_offline=False)
        scope = self.budget.create(budget)
        executor = OnlineExecutor(self.store, workers=workers, record_raw=record_raw,
                                  rate_limit=rate_limit)
        record = dict(run_id=new_id("evolution"), dataset_id=dataset_id,
            val_task_ids=list(val_ids), test_task_ids=list(test_ids),
            gate_profile=protocol.gate_profile if protocol.gate_profile != "custom" else gate_profile,
            max_rounds=max_rounds, budget_cny=budget,
            base_version=version["version_id"], current_version=version["version_id"],
            parent_run_id=None, baseline_run_id=None, scope_id=scope["scope_id"], test_scope_id=None,
            round_index=0, accepted=[], rejected=[], insufficient=[], consecutive_rejections=0,
            rounds=[], status="running", stop_reason=None, cost_cny=0.0, actual_cost_cny=0.0,
            api_calls=0, val_metrics=None, test_metrics=None, started_at=now(), updated_at=now(),
            workers=workers, gate_spec=spec.to_dict(), model_config=model_config,
            external_api=bool(getattr(runner, "external_api", False)),
            protocol=protocol.to_dict(), protocol_hash=protocol.hash,
            protocol_warnings=protocol_warnings(protocol),
            stage=None, test_phase=dict(status="pending", base_run_id=None, final_run_id=None,
                                        started_at=None, protocol_hash=protocol.hash,
                                        warnings=protocol_warnings(protocol)),
            selection=None, rate_limit=getattr(rate_limit, "rate", None),
            duration_s=0.0, trace_path=str(getattr(self.trace, "path", "") or ""))
        began = time.monotonic()
        self._save_evolution(record)
        self.trace.emit("evolve_started", run_id=record["run_id"], protocol_hash=protocol.hash,
                        gate_profile=record["gate_profile"], max_rounds=max_rounds, budget_cny=budget)
        base_run = self._ensure_baseline(record, spec, runner, executor)
        if base_run["status"] != "completed":
            record.update(status="failed", stop_reason="baseline_failed",
                          error=base_run.get("termination_reason"))
            record["duration_s"] = time.monotonic()-began
            self._save_evolution(record)
            self.trace.emit("evolve_failed", run_id=record["run_id"], reason="baseline_failed")
            return self._evolution_result(record, executor)
        self._evolve_rounds(record, spec, proposer, runner, executor, protocol)
        self._evaluate_test(record, spec, runner, executor, protocol)
        record.update(status="completed", duration_s=time.monotonic()-began)
        self._save_evolution(record)
        self.trace.emit("evolve_completed", run_id=record["run_id"],
                        duration_ms=record["duration_s"]*1000, cost=record["cost_cny"],
                        accepted=len(record["accepted"]), rejected=len(record["rejected"]),
                        stop_reason=record["stop_reason"])
        return self._evolution_result(record, executor)

    def resume(self, run_id: str | None = None, proposer=None, runner=None, **kwargs):
        """``resume()`` 恢复存储状态；``resume(run_id, proposer, runner)`` 续跑演化。"""
        if run_id is None:
            version = self.store.current()
            with self.store.connection() as conn:
                self.store.check_memory(conn, version, allow_simulated=True)
                receipts = self.receipts.all(conn)
            return dict(current_version=version,
                active_experiences=self.experiences.search("", allow_simulated=True, active_only=True),
                candidates=self.store.list("candidates"), experiences=self.store.list("experiences"),
                budgets=self.store.list("budgets"), runs=self.store.list("runs"), decisions=self.store.list("decisions"),
                receipts=receipts)
        record = self.store.read(EVOLUTION, run_id)
        if record["status"] != "running":
            raise StateConflict("该演化已结束，不能续跑")
        if proposer is None or runner is None:
            raise ValueError("续跑演化需要重新提供 proposer 与 runner")
        spec = GateSpec.from_dict(record["gate_spec"])
        protocol = (ProtocolSpec.from_dict(record["protocol"]) if record.get("protocol")
                    else ProtocolSpec.from_gate_spec(spec, test_task_ids=record["test_task_ids"]))
        workers = int(kwargs.get("workers") or record.get("workers") or 4)
        executor = OnlineExecutor(self.store, workers=workers,
                                  record_raw=bool(kwargs.get("record_raw", True)),
                                  rate_limit=kwargs.get("rate_limit"))
        began = time.monotonic()
        self.trace.emit("evolve_resumed", run_id=run_id, round_index=record["round_index"],
                        stage=(record.get("stage") or {}).get("step"))
        base_run = self._ensure_baseline(record, spec, runner, executor)
        if base_run["status"] != "completed":
            record.update(status="failed", stop_reason="baseline_failed",
                          error=base_run.get("termination_reason"))
            self._save_evolution(record)
            return self._evolution_result(record, executor)
        self._evolve_rounds(record, spec, proposer, runner, executor, protocol)
        self._evaluate_test(record, spec, runner, executor, protocol)
        record.update(status="completed", duration_s=record.get("duration_s", 0.0)+time.monotonic()-began)
        self._save_evolution(record)
        self.trace.emit("evolve_completed", run_id=run_id, duration_ms=record["duration_s"]*1000,
                        cost=record["cost_cny"], accepted=len(record["accepted"]),
                        rejected=len(record["rejected"]), stop_reason=record["stop_reason"],
                        resumed=True)
        return self._evolution_result(record, executor)

    # ---- 演化内部步骤 ----

    def _check_split(self, dataset: dict, val_ids: tuple, test_ids: tuple):
        by_id = {t["task_id"] for t in dataset["tasks"]}
        if not val_ids or not test_ids:
            raise ValueError("验证集与测试集都不能为空")
        if set(val_ids) & set(test_ids):
            raise ValueError("验证集与测试集不能重叠")
        missing = (set(val_ids) | set(test_ids)) - by_id
        if missing:
            raise ValueError(f"任务不在数据集里：{sorted(missing)[:3]}")
        return val_ids, test_ids

    def _ensure_baseline(self, record: dict, spec: GateSpec, runner, executor) -> dict:
        """基线运行也持久化 run_id；中断后续跑复用同一作用域与运行记录。"""
        run_id = record.get("baseline_run_id") or new_id("run")
        if record.get("baseline_run_id") != run_id:
            record["baseline_run_id"] = run_id
            self._save_evolution(record)
        try:
            existing = self.store.read("runs", run_id)
        except KeyError:
            existing = None
        if existing and existing["status"] == "completed":
            base_run = existing
            record["parent_run_id"] = existing["run_id"]
        else:
            base_run = self.run(record["base_version"], spec, record["scope_id"], runner,
                                executor=executor,
                                resume_run_id=(existing["run_id"] if existing else None),
                                run_id=run_id)
            record["parent_run_id"] = base_run["run_id"]
        record["val_metrics"] = (self._summarize(spec, base_run)
                                 if base_run["status"] == "completed" else None)
        self._save_evolution(record)
        self.trace.emit("baseline_run", run_id=record["run_id"], candidate_id=None,
                        duration_ms=float(base_run.get("seconds") or 0.0)*1000,
                        cost=float(base_run.get("projected_cost_cny") or 0.0),
                        status=base_run["status"], api_calls=base_run.get("api_calls", 0))
        return base_run

    def _failures(self, spec: GateSpec, run_id: str, limit: int = 8) -> tuple[FailureCase, ...]:
        run = self.store.read("runs", run_id)
        dataset = self.store.read("datasets", spec.dataset_id)
        questions = {t["task_id"]: t["question"] for t in dataset["tasks"]}
        by_key = {(r["task_id"], r["repeat"]): r for r in run["outputs"]}
        cases = []
        for task_id in spec.task_ids:
            row = by_key.get((task_id, 1))
            if row is None or row.get("status") != "completed":
                continue
            em, f1 = score(row["answer"], dataset["labels"][task_id])
            if em < 1:
                cases.append(FailureCase(task_id=task_id, question=questions.get(task_id, ""),
                                         prediction=row["answer"], gold=dataset["labels"][task_id],
                                         em=em, f1=f1))
        return tuple(cases[:limit])

    def _summarize(self, spec: GateSpec, run: dict) -> dict:
        with self.store.connection() as conn:
            summary = self.validator.summarize(conn, spec, run)
        return dict(count=summary["count"], em=summary["em"], f1=summary["f1"],
                    cost_cny=run["projected_cost_cny"], api_calls=run["api_calls"],
                    actual_cost_cny=run["actual_api_cost_cny"], run_id=run["run_id"],
                    version_id=run["version_id"])

    def _proposal_context(self, record: dict, spec: GateSpec, index: int) -> ProposalContext:
        parent_version = self.store.version(record["current_version"])
        artifacts = {kind: self.store.load_artifact(ref) for kind, ref in parent_version["artifacts"].items()}
        last_reasons = record["rounds"][-1]["reasons"] if record["rounds"] else []
        return ProposalContext(parent_version=parent_version, artifacts=artifacts, gate=spec,
                               failures=self._failures(spec, record["parent_run_id"]),
                               gate_reasons=tuple(last_reasons), round_index=index)

    def _adopt_pending(self, record: dict, spec: GateSpec) -> str | None:
        """断点落在“登记已提交、阶段未保存”的窗口时，认领那个已登记候选。"""
        seen = {row.get("candidate_id") for row in record["rounds"]}
        stage = record.get("stage") or {}
        seen.add(stage.get("candidate_id"))
        rows = [c for c in self.store.list("candidates")
                if c["status"] == "registered" and c["candidate_id"] not in seen
                and c["parent_version_id"] == record["current_version"]
                and c["validation_config"] == spec.to_dict()]
        if len(rows) > 1:
            raise StateConflict("存在多个未完成的已登记候选，无法判断断点应恢复哪一个")
        return rows[0]["candidate_id"] if rows else None

    def _evolve_rounds(self, record: dict, spec: GateSpec, proposer, runner, executor,
                       protocol: ProtocolSpec) -> None:
        while record["round_index"] < record["max_rounds"]:
            if self.budget.remaining(record["scope_id"]) <= 0:
                record["stop_reason"] = "budget_exhausted"
                self._save_evolution(record)
                break
            if record["consecutive_rejections"] >= 2:
                record["stop_reason"] = "no_improvement"
                self._save_evolution(record)
                break
            self._advance_round(record, spec, proposer, runner, executor, protocol)
            if record.get("stop_reason") in {"proposer_declined", "no_improvement", "budget_exhausted"}:
                break
        if record["round_index"] >= record["max_rounds"] and record["stop_reason"] is None:
            record["stop_reason"] = "max_rounds"
            self._save_evolution(record)

    def _advance_round(self, record: dict, spec: GateSpec, proposer, runner, executor,
                       protocol: ProtocolSpec) -> None:
        index = record["round_index"]
        stage = record.get("stage")
        if not stage or stage.get("round") != index or stage.get("step") not in STEPS:
            stage = dict(round=index, step="propose", candidate_id=None, candidate_version=None,
                         child_run_id=None, started_at=now(), resumed=bool(record.get("stage")))
            record["stage"] = stage
            self._save_evolution(record)
            self.store.checkpoint("evolve.before_register")
        if stage["step"] == "propose":
            candidate_id = stage.get("candidate_id") or self._adopt_pending(record, spec)
            if candidate_id is None:
                proposal = proposer.propose(self._proposal_context(record, spec, index))
                self.trace.emit("round_proposed", run_id=record["run_id"], round=index+1,
                                proposed=proposal is not None,
                                candidate_id=getattr(proposal, "candidate_id", None))
                if proposal is None:
                    self._append_stop_round(record, index)
                    return
                candidate = self.register(proposal, protocol=protocol)
                candidate_id = candidate["candidate_id"]
                stage.update(candidate_id=candidate_id, candidate_version=candidate["version_id"])
            else:
                candidate = self.store.read("candidates", candidate_id)
                stage["resumed"] = True
            stage["step"] = "run"
            stage["started_at"] = stage.get("started_at") or now()
            self._save_evolution(record)
            self.store.checkpoint("evolve.after_register")
        candidate = self.store.read("candidates", stage["candidate_id"])
        if stage["step"] == "run":
            if stage.get("child_run_id") is None:
                stage["child_run_id"] = new_id("run")
                self._save_evolution(record)
            child_run = self._run_child(record, spec, runner, executor, stage, candidate)
            stage.update(step="validate", run_status=child_run["status"])
            self._save_evolution(record)
            self.store.checkpoint("evolve.after_run")
        if stage["step"] == "validate":
            candidate = self.store.read("candidates", stage["candidate_id"])
            if candidate["status"] == "registered":
                report = self.validate(candidate["candidate_id"], record["parent_run_id"],
                                       stage["child_run_id"])
            else:
                report = self.store.read("evaluations", candidate["evaluation_ids"][-1])
                stage["resumed"] = True
            stage["step"] = "decide"
            self._save_evolution(record)
            self.store.checkpoint("evolve.after_validate")
        # decide
        candidate = self.store.read("candidates", stage["candidate_id"])
        report = self.store.read("evaluations", candidate["evaluation_ids"][-1])
        if report.get("passed") and candidate["status"] == "validated":
            self.accept(candidate["candidate_id"])
            candidate = self.store.read("candidates", stage["candidate_id"])
        self._finalize_round(record, candidate, report, stage)
        self._save_evolution(record)
        self.store.checkpoint("evolve.after_accept" if report.get("passed") else "evolve.after_reject")
        self._after_round(record)

    def _run_child(self, record: dict, spec: GateSpec, runner, executor, stage: dict,
                   candidate: dict) -> dict:
        run_id = stage["child_run_id"]
        try:
            existing = self.store.read("runs", run_id)
        except KeyError:
            existing = None
        if existing and existing["status"] == "completed":
            stage["resumed"] = True
            return existing
        child_run = self.run(candidate["version_id"], spec, record["scope_id"], runner,
                             candidate_id=candidate["candidate_id"], executor=executor,
                             resume_run_id=(existing["run_id"] if existing else None),
                             run_id=run_id)
        self.trace.emit("candidate_run", run_id=record["run_id"],
                        candidate_id=candidate["candidate_id"],
                        duration_ms=float(child_run.get("seconds") or 0.0)*1000,
                        cost=float(child_run.get("projected_cost_cny") or 0.0),
                        status=child_run["status"], api_calls=child_run.get("api_calls", 0),
                        round=stage["round"]+1)
        return child_run

    def _finalize_round(self, record: dict, candidate: dict, report: dict, stage: dict) -> None:
        metrics = report.get("metrics") or {}
        verdict = report.get("verdict") or ("pass" if report.get("passed") else "fail")
        passed = bool(report.get("passed"))
        decision = "accept" if passed else ("insufficient" if verdict == "insufficient" else "reject")
        index = stage["round"]
        if passed:
            record["accepted"].append(candidate["candidate_id"])
            record["current_version"] = candidate["version_id"]
            record["parent_run_id"] = stage.get("child_run_id")
            record["consecutive_rejections"] = 0
            if stage.get("child_run_id"):
                child_run = self.store.read("runs", stage["child_run_id"])
                if child_run["status"] == "completed":
                    record["val_metrics"] = self._summarize(
                        GateSpec.from_dict(record["gate_spec"]), child_run)
        else:
            record["rejected"].append(candidate["candidate_id"])
            record["consecutive_rejections"] += 1
            if decision == "insufficient":
                record["insufficient"].append(candidate["candidate_id"])
        statistics = report.get("statistics") or {}
        mcnemar = statistics.get("mcnemar") or {}
        row = dict(
            round=index+1, candidate_id=candidate["candidate_id"], decision=decision,
            verdict=verdict, reasons=list(report.get("reasons") or []),
            profile=gate_profile(GateSpec.from_dict(candidate["validation_config"])),
            run_id=stage.get("child_run_id"), candidate_version=candidate["version_id"],
            parent_version=candidate["parent_version_id"],
            metrics={key: metrics.get(key) for key in
                     ("parent_em", "candidate_em", "delta_em", "delta_f1", "regression_rate",
                      "cost_ratio", "interval", "interval_bca", "regression_count", "count")},
            statistics=dict(mcnemar_p=mcnemar.get("p_value"),
                            mcnemar_method=mcnemar.get("method"),
                            bca=statistics.get("bca"),
                            mde=(statistics.get("mde") or {}).get("mde"),
                            delta_significant=statistics.get("delta_significant"),
                            underpowered=statistics.get("underpowered")),
            evidence=report.get("evidence"), protocol_hash=report.get("protocol_hash"),
            comparison_count=int(candidate.get("comparison_count") or 0),
            resumed=bool(stage.get("resumed")),
            duration_s=_elapsed_since(stage.get("started_at")), created_at=now())
        record["rounds"].append(row)
        record["stage"] = None
        record["round_index"] += 1
        scope = self.budget.read(record["scope_id"])
        record["cost_cny"] = scope["accounted_cny"]
        record["actual_cost_cny"] = scope["actual_api_cost_cny"]
        record["api_calls"] = scope["api_calls"]
        self.trace.emit("round_finalized", run_id=record["run_id"],
                        candidate_id=candidate["candidate_id"], duration_ms=row["duration_s"]*1000,
                        cost=float(metrics.get("candidate_cost_cny") or 0.0),
                        round=index+1, decision=decision, verdict=verdict,
                        delta_em=metrics.get("delta_em"), p_value=mcnemar.get("p_value"))

    def _append_stop_round(self, record: dict, index: int) -> None:
        record["rounds"].append(dict(
            round=index+1, candidate_id=None, decision="stop",
            reasons=["提议器未给出新候选（连续无改进或主动放弃）"],
            profile=gate_profile(GateSpec.from_dict(record["gate_spec"])), run_id=None,
            candidate_version=None, parent_version=record["current_version"], metrics={},
            statistics={}, verdict="stop", protocol_hash=record.get("protocol_hash"),
            duration_s=0.0, created_at=now()))
        record["round_index"] += 1
        record["stop_reason"] = "proposer_declined"
        record["stage"] = None
        self._save_evolution(record)

    def _run_test_version(self, record: dict, test_spec: GateSpec, runner, executor,
                          phase: dict, key: str, version_id: str) -> dict:
        run_id = phase.get(key) or new_id("run")
        phase[key] = run_id
        self._save_evolution(record)
        try:
            existing = self.store.read("runs", run_id)
        except KeyError:
            existing = None
        if existing and existing["status"] == "completed":
            return existing
        run = self.run(version_id, test_spec, record["test_scope_id"], runner, executor=executor,
                       resume_run_id=(existing["run_id"] if existing else None), run_id=run_id)
        self.trace.emit("test_run", run_id=record["run_id"], candidate_id=None,
                        duration_ms=float(run.get("seconds") or 0.0)*1000,
                        cost=float(run.get("projected_cost_cny") or 0.0),
                        status=run["status"], phase_key=key, version_id=version_id)
        return run

    def _evaluate_test(self, record: dict, spec: GateSpec, runner, executor,
                       protocol: ProtocolSpec) -> None:
        """测试阶段：协议守卫 + 只跑一次 + 可续跑；结果只写 test_metrics。"""
        phase = record.get("test_phase") or dict(status="pending", base_run_id=None,
                                                 final_run_id=None, started_at=None,
                                                 protocol_hash=protocol.hash,
                                                 warnings=protocol_warnings(protocol))
        record["test_phase"] = phase
        if phase.get("status") == "done":
            self.trace.emit("test_reused", run_id=record["run_id"],
                            protocol_hash=protocol.hash, reason="phase-done")
            return
        guard = TestSetGuard(self.store)
        phase["guard"] = guard.check(protocol, own_run_ids=[phase.get("base_run_id"),
                                                            phase.get("final_run_id")])
        if record.get("test_scope_id") is None:
            scope = self.budget.create(record["budget_cny"])
            record["test_scope_id"] = scope["scope_id"]
        test_spec = GateSpec.from_dict(dict(spec.to_dict(), task_ids=list(record["test_task_ids"])))
        phase["status"] = "running"
        phase["started_at"] = phase.get("started_at") or now()
        self._save_evolution(record)
        self.trace.emit("test_phase_started", run_id=record["run_id"],
                        protocol_hash=protocol.hash, tasks=len(record["test_task_ids"]))
        base_test = self._run_test_version(record, test_spec, runner, executor, phase,
                                           "base_run_id", record["base_version"])
        if base_test["status"] != "completed":
            record["test_metrics"] = dict(count=len(record["test_task_ids"]), error="测试集基线运行失败",
                                          selection_used_test=False, run_id=base_test["run_id"],
                                          protocol_hash=protocol.hash,
                                          warnings=protocol_warnings(protocol),
                                          reason=base_test.get("termination_reason"))
            phase.update(status="done", completed_at=now())
            self._save_evolution(record)
            return
        summary_base = self._summarize(test_spec, base_test)
        if record["current_version"] == record["base_version"]:
            summary_final, final_test = dict(summary_base), base_test
            phase["final_run_id"] = base_test["run_id"]
        else:
            final_test = self._run_test_version(record, test_spec, runner, executor, phase,
                                                "final_run_id", record["current_version"])
            if final_test["status"] != "completed":
                record["test_metrics"] = dict(count=len(record["test_task_ids"]), error="测试集最终版本运行失败",
                                              selection_used_test=False, run_id=final_test["run_id"],
                                              protocol_hash=protocol.hash,
                                              warnings=protocol_warnings(protocol),
                                              reason=final_test.get("termination_reason"))
                phase.update(status="done", completed_at=now())
                self._save_evolution(record)
                return
            summary_final = self._summarize(test_spec, final_test)
        with self.store.connection() as conn:
            try:
                comparison = self.validator.compare(conn, test_spec, base_test, final_test)
            except Exception:
                comparison = {}
        record["test_metrics"] = dict(
            count=summary_base["count"], baseline=summary_base, candidate=summary_final,
            delta_em=summary_final["em"]-summary_base["em"],
            delta_f1=summary_final["f1"]-summary_base["f1"],
            baseline_run_id=base_test["run_id"], candidate_run_id=final_test["run_id"],
            selection_used_test=False, protocol_hash=protocol.hash,
            test_policy=protocol.test_policy, guard=phase["guard"],
            statistics=comparison.get("statistics"),
            mcnemar=comparison.get("mcnemar"), p_value=comparison.get("p_value"),
            interval=comparison.get("interval"), interval_bca=comparison.get("interval_bca"),
            wilson_baseline=comparison.get("wilson_parent"),
            wilson_candidate=comparison.get("wilson_candidate"),
            mde=comparison.get("mde"), underpowered=comparison.get("underpowered"),
            delta_significant=comparison.get("delta_significant"),
            warnings=protocol_warnings(protocol))
        phase.update(status="done", completed_at=now())
        record["cost_cny"] = (self.budget.read(record["scope_id"])["accounted_cny"]
                              + self.budget.read(record["test_scope_id"])["accounted_cny"])
        record["actual_cost_cny"] = (self.budget.read(record["scope_id"])["actual_api_cost_cny"]
                                     + self.budget.read(record["test_scope_id"])["actual_api_cost_cny"])
        record["api_calls"] = (self.budget.read(record["scope_id"])["api_calls"]
                               + self.budget.read(record["test_scope_id"])["api_calls"])
        self._save_evolution(record)
        self.trace.emit("test_completed", run_id=record["run_id"],
                        cost=float(record["test_metrics"].get("baseline", {}).get("cost_cny") or 0.0),
                        delta_em=record["test_metrics"]["delta_em"], tasks=len(record["test_task_ids"]),
                        p_value=record["test_metrics"].get("p_value"))

    def _evolution_result(self, record: dict, executor) -> dict:
        ids = set(record["accepted"]) | set(record["rejected"])
        with self.store.connection() as conn:
            receipts = [r for r in self.receipts.all(conn) if r["candidate_id"] in ids]
        return dict(run_id=record["run_id"], status=record["status"], stop_reason=record["stop_reason"],
                    gate_profile=record["gate_profile"], max_rounds=record["max_rounds"],
                    budget_cny=record["budget_cny"], base_version=record["base_version"],
                    final_version=record["current_version"], rounds=record["rounds"],
                    accepted=list(record["accepted"]), rejected=list(record["rejected"]),
                    insufficient=list(record.get("insufficient") or []),
                    val_metrics=record["val_metrics"], test_metrics=record["test_metrics"],
                    receipts=receipts, cost_cny=record["cost_cny"],
                    actual_cost_cny=record["actual_cost_cny"], api_calls=record["api_calls"],
                    external_api=bool(record.get("external_api")),
                    selection=record.get("selection"), protocol=record.get("protocol"),
                    protocol_hash=record.get("protocol_hash"),
                    protocol_warnings=record.get("protocol_warnings"),
                    test_phase=record.get("test_phase"),
                    duration_s=record.get("duration_s"), trace_path=record.get("trace_path"))

    def _save_evolution(self, record: dict):
        record["updated_at"] = now()
        record["selection"] = build_summary(record.get("rounds") or [])
        with self.store.transaction("evolution.save") as conn:
            self.store.put(conn, EVOLUTION, record["run_id"], record)
            self.store.event(conn, "evolution_round", dict(run_id=record["run_id"],
                                                           round=record["round_index"],
                                                           accepted=len(record["accepted"]),
                                                           rejected=len(record["rejected"])))

    def _after_round(self, record: dict):
        """轮次提交后的钩子；测试用它注入中断，验证断点续跑。"""
        return None

    # ------------------------------------------------------------------
    # 统计视图
    # ------------------------------------------------------------------

    def stats(self, run_id: str | None = None) -> dict:
        """重算一次演化的统计视图（不改变状态）。"""
        records = self.store.list(EVOLUTION)
        if not records:
            raise KeyError("状态库里没有演化记录")
        if run_id:
            record = self.store.read(EVOLUTION, run_id)
        else:
            record = sorted(records, key=lambda item: item["updated_at"])[-1]
        spec = GateSpec.from_dict(record["gate_spec"])
        test = record.get("test_metrics") or {}
        analysis = test.get("statistics")
        if not analysis and test.get("baseline_run_id") and test.get("candidate_run_id"):
            test_spec = GateSpec.from_dict(dict(spec.to_dict(), task_ids=list(record["test_task_ids"])))
            with self.store.connection() as conn:
                base = self.store.get(conn, "runs", test["baseline_run_id"])
                final = self.store.get(conn, "runs", test["candidate_run_id"])
                analysis = self.validator.compare(conn, test_spec, base, final)["statistics"]
        rounds = []
        for row in record.get("rounds") or []:
            rounds.append(dict(round=row["round"], decision=row["decision"],
                               verdict=row.get("verdict"),
                               delta_em=(row.get("metrics") or {}).get("delta_em"),
                               count=(row.get("metrics") or {}).get("count"),
                               mcnemar_p=(row.get("statistics") or {}).get("mcnemar_p"),
                               mde=(row.get("statistics") or {}).get("mde"),
                               underpowered=(row.get("statistics") or {}).get("underpowered")))
        return dict(run_id=record["run_id"], status=record.get("status"),
                    gate_profile=record.get("gate_profile"),
                    protocol_hash=record.get("protocol_hash"),
                    protocol=record.get("protocol"),
                    selection=record.get("selection") or build_summary(record.get("rounds") or []),
                    rounds=rounds, test=analysis,
                    test_metrics={key: test.get(key) for key in
                                  ("count", "delta_em", "delta_f1", "p_value", "underpowered",
                                   "delta_significant", "warnings") if key in test})
