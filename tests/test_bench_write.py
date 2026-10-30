"""写入压测脚本：小规模跑通、JSON 摘要可解析、WAL 生效。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/"examples"))

import bench_write  # noqa: E402


class BenchWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bench-test-")
        self.addCleanup(self.temp.cleanup)

    def test_json_summary_reports_throughput_and_wal(self):
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = bench_write.main(["--records", "40", "--workers", "4", "--json",
                                     "--state", str(Path(self.temp.name)/"state")])
        self.assertEqual(code, 0)
        summary = json.loads(buffer.getvalue().strip())
        self.assertEqual(summary["records"], 40)
        self.assertEqual(summary["journal_mode"], "WAL")
        self.assertTrue(summary["ok"])
        self.assertGreater(summary["append"]["throughput"], 0)
        self.assertEqual(summary["concurrent"]["errors"], [])
        self.assertEqual(summary["read_during_write"]["errors"], [])

    def test_summary_shape(self):
        root = Path(self.temp.name)/"state2"
        bench_write.main(["--records", "30", "--workers", "3", "--json", "--state", str(root)])
        # 直接调用内部函数，避免捕获 stdout；口径与 CLI 路径一致。
        from harness.storage import FoundationStore
        store = FoundationStore(Path(self.temp.name)/"state3")
        append = bench_write.bench_append(store, 25)
        concurrent = bench_write.bench_concurrent(store, 25, 5)
        readers = bench_write.bench_read_during_write(store, 5, reads=10)
        self.assertEqual(append["records"], 25)
        self.assertGreater(append["throughput"], 0)
        self.assertLessEqual(append["latency_ms"]["p50"], append["latency_ms"]["p95"])
        self.assertEqual(concurrent["records"], 25)
        self.assertEqual(concurrent["errors"], [])
        self.assertEqual(readers["errors"], [])
        self.assertEqual(readers["reads"], 10)
        with store.connection() as conn:
            self.assertEqual(str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower(), "wal")

    def test_rejects_invalid_arguments(self):
        with self.assertRaises(SystemExit):
            bench_write.main(["--records", "0"])


if __name__ == "__main__":
    unittest.main()
