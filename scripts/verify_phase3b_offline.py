"""运行批准范围的公共底座和兼容测试，禁止网络并保存真实逐项结果。"""

import argparse
import json
import os
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ.setdefault("DSPY_CACHEDIR", str(ROOT/".cache/dspy"))
os.environ.pop("DASHSCOPE_API_KEY", None)


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self.started = {}

    def startTest(self, test):
        self.started[test.id()] = time.monotonic()
        super().startTest(test)

    def record(self, test, status, detail=None):
        self.records.append(dict(test_id=test.id(), status=status, detail=detail,
            seconds=time.monotonic()-self.started.get(test.id(), time.monotonic())))

    def addSuccess(self, test):
        super().addSuccess(test)
        self.record(test, "passed")

    def addError(self, test, err):
        super().addError(test, err)
        self.record(test, "error", self._exc_info_to_string(err, test))

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.record(test, "failed", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.record(test, "skipped", reason)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.record(subtest, "subtest_failed", self._exc_info_to_string(err, test))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--suite", choices=["all", "foundation", "compat"], default="all")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if ROOT/"runs/phase3b-offline" not in output.parents:
        parser.error("结果仅可写入新的runs/phase3b-offline目录")
    if output.exists():
        parser.error("已有验收结果不得覆盖，请提供新文件名")
    suite = unittest.TestSuite()
    began = time.monotonic()
    attempts = []

    def deny_network(*args, **kwargs):
        attempts.append("blocked")
        raise RuntimeError("Phase 3-B离线验收禁止网络访问")

    with patch("socket.socket.connect", side_effect=deny_network), patch("socket.create_connection", side_effect=deny_network):
        patterns = {"all": ["test_harness_foundation.py", "test_benchmark.py", "test_round2.py"],
                    "foundation": ["test_harness_foundation.py"], "compat": ["test_benchmark.py", "test_round2.py"]}
        for pattern in patterns[args.suite]:
            suite.addTests(unittest.defaultTestLoader.discover(str(ROOT/"tests"), pattern=pattern))
        result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
    payload = dict(phase="3-B", mode="offline_acceptance", suite=args.suite, completed_at_utc=datetime.now(timezone.utc).isoformat(),
        tests_run=result.testsRun, failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),
        passed=result.wasSuccessful(), seconds=time.monotonic()-began, records=result.records,
        blocked_network_attempts=len(attempts), actual_model_api_calls=0, actual_api_cost_cny=0,
        note="记录的是离线测试结果；模拟预算投影不构成API账单。")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print(json.dumps({k:v for k,v in payload.items() if k != "records"}, ensure_ascii=False))
    return 0 if result.wasSuccessful() and not attempts else 1


if __name__ == "__main__":
    raise SystemExit(main())
