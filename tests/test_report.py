"""报告生成：版本链、门禁理由、凭证与成本必须出现在 Markdown 里。"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import EvolutionFoundation, FoundationStore, RuntimeTask
from harness.proposers.mock import ScriptedProposer
from harness.report import generate, write
from harness.runners.mock import MockRunner

VAL = ["v1", "v2", "v3", "v4"]
TEST = ["t1", "t2"]
LABELS = {t: f"answer {t}" for t in VAL+TEST}
SOLVED = {"base": {"v1", "v2", "t1"}, "bad": {"v1", "t1"}, "good": set(VAL+TEST)}


def responder(task, prompt, artifacts):
    if "先逐跳确定" in prompt:
        group = "good"
    elif "回答尽量简短" in prompt:
        group = "bad"
    else:
        group = "base"
    return LABELS[task.task_id] if task.task_id in SOLVED[group] else "wrong"


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="report-")
        self.addCleanup(self.temp.cleanup)
        self.store = FoundationStore(Path(self.temp.name)/"state")
        self.engine = EvolutionFoundation(self.store)
        artifacts = {
            "prompt": {"components": {"final_answer.predict": "baseline"}},
            "memory": {"experience_ids": [], "retrieval_policy": {}, "lifecycle_policy": {}},
            "tool": {"tools": [], "selection_policy": {}, "risk_constraints": {}},
            "skill": {"definitions": [], "selection_policy": {}, "lifecycle_policy": {}},
        }
        refs = {k: self.store.write_artifact(k, v) for k, v in artifacts.items()}
        self.store.initialize(refs, {"model_config": {"model": "mock", "temperature": 0}})
        self.engine.register_dataset("report-set", [RuntimeTask(t, f"q {t}") for t in VAL+TEST], LABELS)

    def evolve(self, steps=("bad_patch", "good_patch", None)):
        return self.engine.evolve("report-set", VAL, TEST,
                                  ScriptedProposer(self.store, list(steps)),
                                  MockRunner(responder, unit_cost_cny=0.001),
                                  gate_profile="screen", max_rounds=3, budget_cny=5.0)

    def test_report_contains_version_chain_gate_reasons_and_cost(self):
        result = self.evolve()
        text = generate(self.store, evolution_run_id=result["run_id"])
        self.assertIn("## 版本链", text)
        self.assertIn("## 候选与门禁理由", text)
        self.assertIn("## 演化轮次", text)
        self.assertIn("主指标EM增益不足", text)
        self.assertIn("## 决策凭证", text)
        self.assertIn("链校验：通过", text)
        self.assertIn("## 成本", text)
        self.assertIn("预算作用域", text)
        self.assertIn("最终指标", text)
        self.assertIn("测试集（不参与选择）", text)
        self.assertIn("测试集是否参与选择：否", text)
        self.assertIn(result["final_version"], text)

    def test_write_creates_file_and_returns_path(self):
        result = self.evolve()
        target = Path(self.temp.name)/"out"/"report.md"
        path = write(self.store, target, evolution_run_id=result["run_id"])
        self.assertEqual(path, target)
        self.assertTrue(target.is_file())
        self.assertGreater(len(target.read_text(encoding="utf-8")), 500)

    def test_report_on_empty_store_does_not_crash(self):
        text = generate(self.store)
        self.assertIn("## 版本链", text)
        self.assertIn("## 成本", text)
        self.assertIn("链校验：通过", text)

    def test_report_without_evolution_still_lists_candidates(self):
        self.evolve()
        text = generate(self.store)
        self.assertIn("## 演化轮次", text)  # 默认取最近一次演化
        self.assertIn("accept", text)
        self.assertIn("reject", text)


if __name__ == "__main__":
    unittest.main()
