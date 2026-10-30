"""执行器分层：离线确定性执行器与在线（模型）执行器。

两个执行器共享同一套预算、状态与审计路径：

- :class:`OfflineExecutor` 不包含任何模型发送能力，runner 就是一个纯函数；
- :class:`OnlineExecutor` 通过 ``runner.invoke`` 调用模型（真实 HTTP 或
  mock），支持并发、预算预留/结算、断点续跑与每次尝试的审计记录。
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from typing import Protocol

from .budget import BudgetExceeded, BudgetLedger
from .contracts import GateSpec, OfflineRunner, RuntimeTask, content_hash, new_id, now
from .ratelimit import TokenBucket
from .storage import StateConflict


class Executor(Protocol):
    def run(self, tasks: list[RuntimeTask], version_id: str, spec: GateSpec, scope_id: str,
            runner, *, candidate_id: str | None = None, projected_cost_per_task: float = 0.0,
            resume_run_id: str | None = None, run_id: str | None = None) -> dict: ...


class _PlainRunnerResult:
    """把普通 callable runner 的字符串输出包装成统一结果。"""

    def __init__(self, text: str, simulated_cny: float):
        self.text = text
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cost_cny = simulated_cny
        self.simulated_cny = simulated_cny
        self.latency_s = 0.0
        self.attempts = 1
        self.external_api = False
        self.usage_reported = False
        self.raw = None
        self.error = None


def _invoke(runner, task, version, artifacts, simulated_fallback: float):
    """统一调用接口：ModelRunner.invoke 或普通 callable。"""
    if hasattr(runner, "invoke"):
        return runner.invoke(task, version, artifacts)
    started = time.monotonic()
    text = runner(task, version, artifacts)
    if not isinstance(text, str):
        raise TypeError("执行器输出必须为字符串")
    result = _PlainRunnerResult(text, simulated_fallback)
    result.latency_s = time.monotonic()-started
    return result


def _reservation(runner, task, version, artifacts, fallback: float) -> float:
    if hasattr(runner, "reservation_amount"):
        return float(runner.reservation_amount(task, version, artifacts))
    return float(fallback)


class OfflineExecutor:
    def __init__(self, store):
        self.store = store
        self.ledger = BudgetLedger(store)

    def run(self, tasks: list[RuntimeTask], version_id: str, spec: GateSpec, scope_id: str,
            runner: OfflineRunner, *, candidate_id: str | None = None, projected_cost_per_task: float = 0,
            resume_run_id: str | None = None, run_id: str | None = None) -> dict:
        if resume_run_id is not None:
            raise ValueError("离线执行器不支持断点续跑；请使用在线执行器")
        if [t.task_id for t in tasks] != list(spec.task_ids):
            raise ValueError("执行任务顺序与冻结验证清单不一致")
        version = self.store.version(version_id)
        if version["config_snapshot"]["model_config"] != spec.model_config or version["evaluator_id"] != spec.evaluator_id:
            raise StateConflict("执行配置与冻结版本不一致")
        if candidate_id:
            candidate = self.store.read("candidates", candidate_id)
            if candidate["version_id"] != version_id or candidate["validation_config"] != spec.to_dict():
                raise StateConflict("候选版本/验证配置不匹配")
        elif version["status"] != "accepted":
            raise StateConflict("非接受版本须通过候选路径执行")
        with self.store.connection() as conn:
            if version["status"] == "accepted":
                self.store.check_memory(conn, version, allow_simulated=True)
        artifacts = {kind: self.store.load_artifact(ref) for kind, ref in version["artifacts"].items()}
        run_id = run_id or new_id("run")
        record = dict(run_id=run_id, version_id=version_id, candidate_id=candidate_id, mode="simulation",
            evaluator_id=spec.evaluator_id, validation_config=spec.to_dict(), condition_hash=content_hash(spec.to_dict()),
            scope_id=scope_id, status="running", outputs=[], started_at=now(), seconds=0.0,
            api_calls=0, actual_api_cost_cny=0.0, projected_cost_cny=0.0, termination_reason=None,
            executor="offline", workers=1, external_api=False)
        self._save(record)
        began = time.monotonic()
        for repeat in range(1, spec.repeats+1):
            for task in tasks:
                tick = time.monotonic()
                try:
                    attempt = self.ledger.reserve(scope_id, run_id, version_id, candidate_id, projected_cost_per_task)
                except BudgetExceeded as exc:
                    record.update(status="stopped", termination_reason=str(exc))
                    break
                row = dict(task_id=task.task_id, repeat=repeat, input=asdict(task), attempt_id=attempt,
                           status="completed", started_at=now())
                try:
                    answer = runner(task, version, artifacts)
                    if not isinstance(answer, str):
                        raise TypeError("离线执行器输出必须为字符串")
                    row.update(answer=answer, raw_output=answer)
                except Exception as exc:
                    row.update(status="error", answer="", raw_output="", error=f"{type(exc).__name__}: {exc}")
                    record.update(status="failed", termination_reason=row["error"])
                row.update(seconds=time.monotonic()-tick, ended_at=now())
                self.ledger.settle(attempt, projected_cost_per_task, row["seconds"])
                record["outputs"].append(row)
                record["projected_cost_cny"] += projected_cost_per_task
                self._save(record)
                if row["status"] != "completed":
                    break
            if record["status"] != "running":
                break
        if record["status"] == "running":
            record["status"] = "completed"
        record.update(seconds=time.monotonic()-began, ended_at=now())
        self._save(record)
        return record

    def _save(self, record):
        with self.store.transaction("run.save") as conn:
            self.store.put(conn, "runs", record["run_id"], record)


class OnlineExecutor:
    """并发执行 + 预算预留/结算 + 断点续跑；runner 决定是否真的发请求。

    并发数优先取 ``runner.workers``；预算在每次调用前预留，返回后按 usage
    结算。没有 usage 时按预留上界记账并标记 ``usage_reported=False``。
    """

    def __init__(self, store, *, workers: int = 4, record_raw: bool = True, rate_limit=None):
        if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
            raise ValueError("workers 必须为正整数")
        self.store = store
        self.workers = workers
        self.record_raw = record_raw
        self.ledger = BudgetLedger(store)
        if rate_limit is None or isinstance(rate_limit, TokenBucket):
            self.rate_limiter = rate_limit
        elif isinstance(rate_limit, (int, float)) and not isinstance(rate_limit, bool):
            if rate_limit <= 0:
                raise ValueError("限流速率必须为正")
            self.rate_limiter = TokenBucket(float(rate_limit))
        else:
            raise ValueError("rate_limit 必须是 TokenBucket 或每秒请求数")

    def run(self, tasks: list[RuntimeTask], version_id: str, spec: GateSpec, scope_id: str,
            runner, *, candidate_id: str | None = None, projected_cost_per_task: float = 0.0,
            resume_run_id: str | None = None, run_id: str | None = None) -> dict:
        if [t.task_id for t in tasks] != list(spec.task_ids):
            raise ValueError("执行任务顺序与冻结验证清单不一致")
        version = self.store.version(version_id)
        if version["config_snapshot"]["model_config"] != spec.model_config or version["evaluator_id"] != spec.evaluator_id:
            raise StateConflict("执行配置与冻结版本不一致")
        if candidate_id:
            candidate = self.store.read("candidates", candidate_id)
            if candidate["version_id"] != version_id or candidate["validation_config"] != spec.to_dict():
                raise StateConflict("候选版本/验证配置不匹配")
        elif version["status"] != "accepted":
            raise StateConflict("非接受版本须通过候选路径执行")
        with self.store.connection() as conn:
            if version["status"] == "accepted":
                self.store.check_memory(conn, version, allow_simulated=True)
        artifacts = {kind: self.store.load_artifact(ref) for kind, ref in version["artifacts"].items()}
        external = bool(getattr(runner, "external_api", False))
        workers = int(getattr(runner, "workers", self.workers) or self.workers)
        limiter = self.rate_limiter or getattr(runner, "rate_limiter", None)
        rate_value = getattr(self.rate_limiter, "rate", None)
        if rate_value is None and limiter is not None:
            rate_value = getattr(limiter, "rate", None)

        if resume_run_id:
            record = self.store.read("runs", resume_run_id)
            if record["version_id"] != version_id or record["condition_hash"] != content_hash(spec.to_dict()) \
                    or record["scope_id"] != scope_id or record["candidate_id"] != candidate_id:
                raise StateConflict("断点续跑条件与已有运行不一致")
            if record["status"] not in {"running", "failed", "stopped"}:
                raise StateConflict("该运行已结束，不能续跑")
            record.update(status="running", termination_reason=None, executor="online",
                          workers=workers, external_api=external, mode="real" if external else "simulation")
            dropped = [row for row in record["outputs"] if row.get("status") != "completed"]
            if dropped:
                # 重新尝试失败/中断的 (task_id, repeat)：移除旧行并回退对应计数，
                # 保证“输出行 ↔ 结算尝试”的对应关系在续跑后仍然自洽。
                record["outputs"] = [row for row in record["outputs"] if row.get("status") == "completed"]
                record["projected_cost_cny"] = max(0.0, record["projected_cost_cny"]-sum(
                    float(row.get("cost_cny") or 0.0) for row in dropped))
                record["actual_api_cost_cny"] = max(0.0, record["actual_api_cost_cny"]-sum(
                    float(row.get("cost_cny") or 0.0) for row in dropped if row.get("external_api")))
                record["api_calls"] = max(0, record["api_calls"]-sum(
                    int(row.get("attempts") or 0) for row in dropped))
        else:
            record = dict(run_id=run_id or new_id("run"), version_id=version_id, candidate_id=candidate_id,
                mode="real" if external else "simulation",
                evaluator_id=spec.evaluator_id, validation_config=spec.to_dict(),
                condition_hash=content_hash(spec.to_dict()), scope_id=scope_id, status="running",
                outputs=[], started_at=now(), seconds=0.0, api_calls=0, actual_api_cost_cny=0.0,
                projected_cost_cny=0.0, termination_reason=None, executor="online",
                workers=workers, external_api=external, runner=type(runner).__name__,
                rate_limit_rps=rate_value)
        done = {(row["task_id"], row["repeat"]) for row in record["outputs"] if row["status"] == "completed"}
        self._save(record)
        began = time.monotonic()
        jobs = [(repeat, task) for repeat in range(1, spec.repeats+1) for task in tasks
                if (task.task_id, repeat) not in done]
        stop = {"stop": False, "reason": None}

        def work(repeat, task):
            tick = time.monotonic()
            if limiter is not None:
                limiter.acquire()
            amount = _reservation(runner, task, version, artifacts, projected_cost_per_task)
            try:
                attempt = self.ledger.reserve(scope_id, record["run_id"], version_id, candidate_id,
                                              amount, mode="real" if external else "simulation")
            except BudgetExceeded as exc:
                return dict(budget=True, reason=str(exc), task=task.task_id, repeat=repeat)
            row = dict(task_id=task.task_id, repeat=repeat, input=asdict(task), attempt_id=attempt,
                       status="completed", started_at=now(), external_api=external)
            result = None
            try:
                result = _invoke(runner, task, version, artifacts, amount)
                if result.error:
                    raise RuntimeError(result.error)
                row.update(answer=result.text, raw_output=result.text if self.record_raw else None,
                           prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
                           usage_reported=result.usage_reported, attempts=result.attempts,
                           model=getattr(runner, "model", None))
            except Exception as exc:
                row.update(status="error", answer="", raw_output=None,
                           attempts=getattr(result, "attempts", 0),
                           error=f"{type(exc).__name__}: {exc}")
            row.update(seconds=time.monotonic()-tick, ended_at=now())
            actual = float(getattr(result, "cost_cny", 0.0) or 0.0) if external else 0.0
            calls = int(getattr(result, "attempts", 0) or 0)
            settled = actual if external else float(getattr(result, "simulated_cny", amount) or 0.0)
            if row["status"] == "error":
                settled = actual if external else 0.0
            overrun = settled > amount+1e-12
            # 预留上界一般覆盖实际费用；若仍超出的极端情况，按预留下界记账并显式标记，
            # 避免账本与运行记录不一致（成本信息保留在 row.cost_cny）。
            booked = min(settled, amount)
            self.ledger.settle(attempt, booked, row["seconds"],
                               actual_cost_cny=booked if external else 0.0,
                               api_calls=calls, mode="real" if external else "simulation",
                               usage=dict(prompt_tokens=row.get("prompt_tokens", 0),
                                          completion_tokens=row.get("completion_tokens", 0),
                                          usage_reported=row.get("usage_reported", False)))
            row["cost_cny"] = settled
            row["cost_overrun"] = overrun
            return dict(budget=False, row=row, settled=booked,
                        actual=booked if external else 0.0, calls=calls)

        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs) or 1))) as pool:
            futures = [pool.submit(work, repeat, task) for repeat, task in jobs]
            for future in as_completed(futures):
                try:
                    outcome = future.result()
                except Exception as exc:  # 结算/持久化故障：保留已完成输出，标记失败
                    record.update(status="failed", termination_reason=f"{type(exc).__name__}: {exc}")
                    stop["stop"] = True
                    for other in futures:
                        other.cancel()
                    continue
                if outcome.get("budget"):
                    record.update(status="stopped", termination_reason=outcome["reason"])
                    stop["stop"], stop["reason"] = True, outcome["reason"]
                    for other in futures:
                        other.cancel()
                    continue
                if outcome.get("row") is None:
                    continue
                record["outputs"].append(outcome["row"])
                record["projected_cost_cny"] += outcome["settled"]
                record["actual_api_cost_cny"] += outcome["actual"]
                record["api_calls"] += outcome["calls"]
                if outcome["row"]["status"] != "completed":
                    record.update(status="failed", termination_reason=outcome["row"].get("error"))
                    stop["stop"] = True
                    for other in futures:
                        other.cancel()
                self._save(record)
                self.store.checkpoint(
                    "executor.after_output." + ("candidate" if candidate_id else "baseline"))
        if record["status"] == "running":
            record["status"] = "completed"
        order = {t.task_id: i for i, t in enumerate(tasks)}
        record["outputs"].sort(key=lambda r: (r["repeat"], order[r["task_id"]]))
        record.update(seconds=time.monotonic()-began, ended_at=now(),
                      resumed_from=resume_run_id or record.get("resumed_from"))
        self._save(record)
        self.store.checkpoint("executor.run_completed")
        return record

    def _save(self, record):
        with self.store.transaction("run.save") as conn:
            self.store.put(conn, "runs", record["run_id"], record)
