"""预算作用域账本：预留/结算，模拟费用与真实 API 费用分开统计。"""

import json
from pathlib import Path

from .contracts import new_id, nonnegative, now
from .storage import StateConflict


class BudgetExceeded(RuntimeError):
    pass


def read_legacy_usage(path: str | Path) -> list[dict]:
    """只读旧/新JSONL格式，不隐式加载其它轮次或生成新API账单。"""
    rows = []
    for line_no, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        raw = json.loads(line)
        parameters = raw.get("parameters", {})
        rows.append(dict(source_path=str(Path(path).resolve()), source_line=line_no, historical=True,
            run_id=raw.get("phase"), version_id=raw.get("version_id"), candidate_id=raw.get("candidate_id"),
            model=parameters.get("model", raw.get("model")), calls=1, seconds=raw.get("seconds", 0),
            cost_cny=nonnegative(raw["cost_cny"], "历史费用"), cost_kind="historical_estimate",
            original_cost_kind=raw.get("cost_kind"), error=raw.get("error")))
    return rows


class BudgetLedger:
    """一个作用域 = 一次实验/一次发布流程的硬上限。

    ``accounted_cny`` 是预算消耗（离线为模拟投影，在线为真实/上界费用）；
    ``simulated_cny`` 与 ``real_cny`` 分开累计，避免把模拟费用说成真实账单。
    """

    def __init__(self, store):
        self.store = store

    def create(self, limit_cny: float, *, scope_id: str | None = None) -> dict:
        scope_id = scope_id or new_id("budget")
        scope = dict(scope_id=scope_id, mode="simulation", limit_cny=nonnegative(limit_cny, "预算上限"),
            accounted_cny=0.0, reserved_cny=0.0, actual_api_cost_cny=0.0, api_calls=0,
            simulated_calls=0, simulated_cny=0.0, real_cny=0.0,
            status="open", termination_reason=None, created_at=now())
        with self.store.transaction("budget.create") as conn:
            self.store.put(conn, "budgets", scope_id, scope, insert=True)
        return scope

    def reserve(self, scope_id: str, run_id: str, version_id: str, candidate_id: str | None,
                projected_cost_cny: float, *, mode: str = "simulation") -> str:
        amount = nonnegative(projected_cost_cny, "预留费用")
        if mode not in {"simulation", "real"}:
            raise ValueError("未知执行模式")
        error = None
        attempt = new_id("attempt")
        with self.store.transaction("budget.reserve") as conn:
            scope = self.store.get(conn, "budgets", scope_id)
            if scope["status"] != "open" or scope["accounted_cny"]+scope["reserved_cny"]+amount > scope["limit_cny"]+1e-12:
                scope.update(status="stopped", termination_reason="预算不足，停止新增模拟执行")
                self.store.put(conn, "budgets", scope_id, scope)
                self.store.event(conn, "budget_stopped", dict(scope_id=scope_id, run_id=run_id))
                error = BudgetExceeded(scope["termination_reason"])
            else:
                scope["reserved_cny"] += amount
                self.store.put(conn, "budgets", scope_id, scope)
                self.store.put(conn, "attempts", attempt, dict(attempt_id=attempt, scope_id=scope_id,
                    run_id=run_id, version_id=version_id, candidate_id=candidate_id, mode=mode,
                    reserved_cny=amount, projected_cost_cny=None, actual_api_cost_cny=0.0, api_calls=0,
                    usage=None, status="reserved", created_at=now()), insert=True)
        if error:
            raise error
        return attempt

    def settle(self, attempt_id: str, projected_cost_cny: float, seconds: float, *,
               actual_cost_cny: float = 0.0, api_calls: int = 0, mode: str = "simulation",
               usage: dict | None = None):
        """结算一个执行槽位。

        离线：``projected_cost_cny`` 是模拟费用，只累计 ``simulated_cny``。
        在线：``projected_cost_cny`` 是计入预算的费用（真实费用或上界），同时累计
        ``actual_api_cost_cny`` / ``api_calls`` / ``real_cny``。
        """
        cost = nonnegative(projected_cost_cny, "结算费用")
        seconds = nonnegative(seconds, "执行时间")
        actual = nonnegative(actual_cost_cny, "真实费用")
        if isinstance(api_calls, bool) or not isinstance(api_calls, int) or api_calls < 0:
            raise ValueError("api_calls 必须为非负整数")
        if mode not in {"simulation", "real"}:
            raise ValueError("未知执行模式")
        with self.store.transaction("budget.settle") as conn:
            attempt = self.store.get(conn, "attempts", attempt_id)
            if attempt["status"] != "reserved":
                raise StateConflict("执行槽位已结算，不允许重复计费")
            if cost > attempt["reserved_cny"]+1e-12:
                raise BudgetExceeded("结算超过预留，保留在途状态，不接受候选")
            scope = self.store.get(conn, "budgets", attempt["scope_id"])
            scope["reserved_cny"] = max(0.0, scope["reserved_cny"]-attempt["reserved_cny"])
            scope["accounted_cny"] += cost
            scope["api_calls"] += api_calls
            if mode == "real":
                scope["actual_api_cost_cny"] += actual
                scope["real_cny"] = scope.get("real_cny", 0.0)+cost
            else:
                scope["simulated_calls"] += 1
                scope["simulated_cny"] = scope.get("simulated_cny", 0.0)+cost
            attempt.update(status="settled", projected_cost_cny=cost, seconds=seconds,
                           actual_api_cost_cny=actual, api_calls=api_calls, mode=mode,
                           usage=usage, ended_at=now())
            self.store.put(conn, "attempts", attempt_id, attempt)
            self.store.put(conn, "budgets", scope["scope_id"], scope)

    def stop(self, scope_id: str, reason: str) -> dict:
        with self.store.transaction("budget.stop") as conn:
            scope = self.store.get(conn, "budgets", scope_id)
            scope.update(status="stopped", termination_reason=reason)
            self.store.put(conn, "budgets", scope_id, scope)
            self.store.event(conn, "budget_stopped", dict(scope_id=scope_id, reason=reason))
        return scope

    def remaining(self, scope_id: str) -> float:
        scope = self.read(scope_id)
        return max(0.0, scope["limit_cny"]-scope["accounted_cny"]-scope["reserved_cny"])

    def read(self, scope_id):
        return self.store.read("budgets", scope_id)
