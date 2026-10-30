"""可观测：JSONL trace 的固定字段、过滤、摘要与抗损坏能力。"""

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.trace import CORE_FIELDS, TraceLog, read_events


class TraceLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="trace-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/"trace"/"events.jsonl"

    def test_emit_writes_one_json_line_with_core_fields(self):
        log = TraceLog(self.path)
        row = log.emit("candidate_registered", run_id="run-1", candidate_id="cand-1",
                       duration_ms=12.5, cost=0.001, round=2)
        self.assertTrue(self.path.is_file())
        lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        stored = json.loads(lines[0])
        self.assertEqual(stored, row)
        for field in CORE_FIELDS:
            self.assertIn(field, stored)
        self.assertEqual(stored["event"], "candidate_registered")
        self.assertEqual(stored["run_id"], "run-1")
        self.assertEqual(stored["candidate_id"], "cand-1")
        self.assertEqual(stored["duration_ms"], 12.5)
        self.assertEqual(stored["cost"], 0.001)
        self.assertEqual(stored["round"], 2)
        self.assertIsNotNone(stored["ts"])

    def test_events_filter_by_name_and_limit(self):
        log = TraceLog(self.path)
        log.emit("round_proposed", round=1)
        log.emit("candidate_run", round=1)
        log.emit("round_proposed", round=2)
        log.emit("round_proposed", round=3)
        self.assertEqual(len(log.events()), 4)
        proposed = log.events(event="round_proposed")
        self.assertEqual([row["round"] for row in proposed], [1, 2, 3])
        self.assertEqual(len(log.events(limit=2)), 2)
        self.assertEqual(log.events(limit=2)[-1]["round"], 3)

    def test_summary_aggregates_counts_duration_and_cost(self):
        log = TraceLog(self.path)
        log.emit("candidate_run", duration_ms=100.0, cost=0.01)
        log.emit("candidate_run", duration_ms=50.0, cost=0.02)
        log.emit("candidate_accepted", duration_ms=5.0)
        summary = log.summary()
        self.assertEqual(summary["schema"], "trace/1")
        self.assertEqual(summary["events"], 3)
        self.assertEqual(summary["by_event"]["candidate_run"]["count"], 2)
        self.assertAlmostEqual(summary["by_event"]["candidate_run"]["duration_ms"], 150.0)
        self.assertAlmostEqual(summary["by_event"]["candidate_run"]["cost"], 0.03)
        self.assertAlmostEqual(summary["total_duration_ms"], 155.0)
        self.assertEqual(summary["write_errors"], 0)
        self.assertIsNotNone(summary["first_ts"])

    def test_disabled_trace_returns_row_but_writes_nothing(self):
        log = TraceLog(self.path, enabled=False)
        row = log.emit("evolve_started", run_id="run-9")
        self.assertEqual(row["event"], "evolve_started")
        self.assertFalse(self.path.exists())
        self.assertEqual(log.events(), [])
        self.assertEqual(log.summary()["events"], 0)

    def test_pathless_trace_is_disabled(self):
        log = TraceLog(None)
        log.emit("x")
        self.assertFalse(log.enabled)
        self.assertEqual(log.events(), [])

    def test_reserved_field_collision_is_refused(self):
        log = TraceLog(self.path)
        with self.assertRaises((ValueError, TypeError)):
            log.emit("x", cost=1.0, **{"event": "y"})
        with self.assertRaises(ValueError):
            log.emit("x", duration_ms=1.0, **{"ts": "now"})
        with self.assertRaises(ValueError):
            log.emit("")
        with self.assertRaises(ValueError):
            log.emit(None)

    def test_non_serializable_field_is_refused_before_write(self):
        log = TraceLog(self.path)
        with self.assertRaises(ValueError):
            log.emit("x", payload=object())
        with self.assertRaises(ValueError):
            log.emit("x", payload=float("nan"))
        self.assertEqual(log.events(), [])

    def test_corrupt_lines_are_skipped_not_crashing(self):
        log = TraceLog(self.path)
        log.emit("good", run_id="r1")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("{not json}\n")
            handle.write(json.dumps({"event": "missing-core"})+"\n")
            handle.write("\n")
        log.emit("good", run_id="r2")
        rows = log.events()
        self.assertEqual([row["run_id"] for row in rows], ["r1", "r2"])

    def test_truncate_empties_the_file(self):
        log = TraceLog(self.path)
        log.emit("a")
        log.emit("b")
        log.truncate()
        self.assertEqual(log.events(), [])

    def test_read_events_is_stateless(self):
        log = TraceLog(self.path)
        log.emit("probe", run_id="run-1", note="hi")
        rows = read_events(self.path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["note"], "hi")
        self.assertEqual(read_events(Path(self.temp.name)/"absent.jsonl"), [])

    def test_concurrent_writes_produce_only_valid_lines(self):
        log = TraceLog(self.path)

        def worker(index):
            for step in range(25):
                log.emit("worker", run_id=f"run-{index}", step=step)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        rows = log.events()
        self.assertEqual(len(rows), 200)
        self.assertEqual(len(self.path.read_text(encoding="utf-8").strip().splitlines()), 200)
        self.assertEqual(log.write_errors, 0)


if __name__ == "__main__":
    unittest.main()
