"""CLI：帮助、初始化、单次运行、闭环、报告与错误信息。"""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.cli import main

COMMANDS = ["init", "run", "evolve", "report", "inspect", "receipts", "verify-receipts",
            "rollback", "stats", "trace", "protocol-export", "protocol-import", "protocol-show"]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cli-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root/"state"
        self.dataset = self.root/"dataset.json"
        tasks = [dict(task_id=f"q{i}", question=f"question {i}") for i in range(1, 7)]
        self.dataset.write_text(json.dumps({
            "dataset_id": "cli-ds", "tasks": tasks,
            "labels": {f"q{i}": f"answer {i}" for i in range(1, 7)},
            "val_task_ids": ["q1", "q2", "q3"], "test_task_ids": ["q4", "q5", "q6"]},
            ensure_ascii=False), encoding="utf-8")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_every_command_has_help(self):
        for command in COMMANDS:
            with self.subTest(command=command):
                result = subprocess.run([sys.executable, "-m", "harness.cli", command, "--help"],
                                        cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)

    def test_init_creates_state_and_refuses_overwrite(self):
        code, out, _ = self.run_cli("init", "--db", str(self.state))
        self.assertEqual(code, 0)
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(payload["version_id"].startswith("version-"))
        self.assertTrue((self.state/"foundation.sqlite3").is_file())
        code, _, err = self.run_cli("init", "--db", str(self.state))
        self.assertEqual(code, 2)
        self.assertIn("不会覆盖", err)

    def test_run_mock_writes_summary(self):
        self.run_cli("init", "--db", str(self.state))
        target = self.root/"run.json"
        code, out, err = self.run_cli("run", "--db", str(self.state), "--dataset", str(self.dataset),
                                      "--runner", "mock", "--output", str(target))
        self.assertEqual(code, 0, err)
        payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["tasks"], 6)
        self.assertGreater(payload["api_calls"], 0)
        self.assertGreater(payload["cost_cny"], 0)

    def test_evolve_then_report_and_verify(self):
        self.run_cli("init", "--db", str(self.state))
        report = self.root/"report.md"
        target = self.root/"evolve.json"
        code, out, err = self.run_cli("evolve", "--db", str(self.state), "--dataset", str(self.dataset),
                                      "--proposer", "mock", "--runner", "mock", "--rounds", "2",
                                      "--budget", "1", "--gate", "screen",
                                      "--output", str(target), "--report", str(report))
        self.assertEqual(code, 0, err)
        payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(len(payload["rounds"]), 2)
        self.assertTrue(report.is_file())
        self.assertIn("## 版本链", report.read_text(encoding="utf-8"))
        code, out, _ = self.run_cli("verify-receipts", "--db", str(self.state))
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["verified"])

        code, out, _ = self.run_cli("report", "--db", str(self.state), "--out", str(self.root/"r2.md"))
        self.assertEqual(code, 0)
        self.assertIn("report", json.loads(out))

    def test_stats_trace_and_protocol_round_trip(self):
        self.run_cli("init", "--db", str(self.state))
        target = self.root/"evolve.json"
        code, _, err = self.run_cli("evolve", "--db", str(self.state), "--dataset", str(self.dataset),
                                    "--proposer", "mock", "--runner", "mock", "--rounds", "2",
                                    "--budget", "1", "--gate", "screen", "--output", str(target))
        self.assertEqual(code, 0, err)

        code, out, err = self.run_cli("stats", "--db", str(self.state))
        self.assertEqual(code, 0, err)
        stats = json.loads(out)
        self.assertEqual(stats["status"], "completed")
        self.assertIn("selection", stats)
        self.assertIn("rounds", stats)
        self.assertIsNotNone(stats["protocol_hash"])

        code, out, err = self.run_cli("trace", "--db", str(self.state))
        self.assertEqual(code, 0, err)
        summary = json.loads(out)
        self.assertGreater(summary["events"], 0)
        self.assertIn("evolve_started", summary["by_event"])

        code, out, err = self.run_cli("trace", "--db", str(self.state), "--raw", "--limit", "3")
        self.assertEqual(code, 0, err)
        rows = [json.loads(line) for line in out.strip().splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all("event" in row and "ts" in row for row in rows))

        exported = self.root/"protocol.json"
        code, out, err = self.run_cli("protocol-export", "--db", str(self.state), "--out", str(exported))
        self.assertEqual(code, 0, err)
        self.assertTrue(exported.is_file())
        protocol_hash = json.loads(out)["protocol_hash"]

        code, out, err = self.run_cli("protocol-import", "--db", str(self.state), "--file", str(exported))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["protocol_hash"], protocol_hash)

        code, out, err = self.run_cli("protocol-show", "--db", str(self.state))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["protocol_hash"], protocol_hash)

    def test_missing_state_is_readable_error(self):
        code, _, err = self.run_cli("inspect", "--db", str(self.root/"missing"))
        self.assertEqual(code, 2)
        self.assertIn("状态目录不存在", err)

    def test_bad_dataset_is_readable_error(self):
        self.run_cli("init", "--db", str(self.state))
        bad = self.root/"bad.json"
        bad.write_text(json.dumps({"tasks": [], "labels": {}}), encoding="utf-8")
        code, _, err = self.run_cli("run", "--db", str(self.state), "--dataset", str(bad), "--runner", "mock")
        self.assertEqual(code, 2)
        self.assertIn("数据集", err)


if __name__ == "__main__":
    unittest.main()
