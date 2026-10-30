"""可观测：JSONL 事件日志（trace/1）。

每行是一个自包含 JSON 对象，固定包含 ``event`` / ``ts`` / ``run_id`` /
``candidate_id`` / ``duration_ms`` / ``cost`` 六个字段，其余字段按事件追加。
固定字段让日志可以直接喂给 grep、jq 或任意列式后端，不需要先理解业务。

默认关闭；``EvolutionFoundation`` 会在状态目录下自动启用
``trace/events.jsonl``。写日志失败不会中断主流程（可观测性不能成为新的
单点故障），但会记录 ``trace_error`` 计数供自检。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "trace/1"
CORE_FIELDS = ("event", "ts", "run_id", "candidate_id", "duration_ms", "cost")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class TraceLog:
    """线程安全的 append-only JSONL 事件日志。"""

    def __init__(self, path: str | Path | None = None, *, enabled: bool = True,
                 clock=time.time):
        self.path = Path(path) if path is not None else None
        self.enabled = bool(enabled and self.path is not None)
        self.clock = clock
        self._lock = threading.Lock()
        self.write_errors = 0
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, *, run_id: str | None = None, candidate_id: str | None = None,
             duration_ms: float | None = None, cost: float | None = None, **fields) -> dict:
        """写一条事件并返回落盘对象；关闭或写失败时仍然返回对象。"""
        if not isinstance(event, str) or not event:
            raise ValueError("事件名不能为空")
        row = dict(event=event, ts=_timestamp(), run_id=run_id, candidate_id=candidate_id,
                   duration_ms=None if duration_ms is None else round(float(duration_ms), 3),
                   cost=None if cost is None else float(cost))
        for key, value in fields.items():
            if key in row:
                raise ValueError(f"事件字段与固定字段冲突：{key}")
            row[key] = value
        try:
            json.dumps(row, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"事件字段无法序列化为 JSON：{exc}") from exc
        if not self.enabled:
            return row
        line = json.dumps(row, ensure_ascii=False, allow_nan=False)
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                self.write_errors += 1
        return row

    def events(self, *, event: str | None = None, limit: int | None = None) -> list[dict]:
        """读取事件；坏行跳过并计数，不掩盖日志损坏。"""
        if self.path is None or not self.path.is_file():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or not all(key in row for key in CORE_FIELDS):
                continue
            if event is None or row["event"] == event:
                rows.append(row)
        if limit is not None:
            rows = rows[-int(limit):]
        return rows

    def summary(self) -> dict:
        rows = self.events()
        by_event: dict[str, dict] = {}
        for row in rows:
            bucket = by_event.setdefault(row["event"], dict(count=0, duration_ms=0.0, cost=0.0))
            bucket["count"] += 1
            bucket["duration_ms"] += float(row.get("duration_ms") or 0.0)
            bucket["cost"] += float(row.get("cost") or 0.0)
        for bucket in by_event.values():
            bucket["duration_ms"] = round(bucket["duration_ms"], 3)
            bucket["cost"] = round(bucket["cost"], 8)
        return dict(schema=SCHEMA, path=str(self.path) if self.path else None,
                    enabled=self.enabled, events=len(rows), by_event=dict(sorted(by_event.items())),
                    total_duration_ms=round(sum(float(r.get("duration_ms") or 0) for r in rows), 3),
                    total_cost=round(sum(float(r.get("cost") or 0) for r in rows), 8),
                    first_ts=rows[0]["ts"] if rows else None,
                    last_ts=rows[-1]["ts"] if rows else None,
                    write_errors=self.write_errors)

    def truncate(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self.path.write_text("", encoding="utf-8")


def read_events(path: str | Path) -> list[dict]:
    """无状态读取：把日志文件当作纯数据源。"""
    return TraceLog(path).events()
