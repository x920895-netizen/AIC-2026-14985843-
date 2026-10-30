"""本地 EM 与 DSPy answer_exact_match 的等价性（装有 dspy 时逐例校验）。

harness 用本地 HotpotQA 规范化实现 EM，避免演示与门禁回放背上 7 秒级的
DSPy 导入；这里锁定两者在同一批样例上的结论一致。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.evaluation import exact_match, score

CASES = [
    ("Paris", "Paris"),
    ("the Paris", "Paris"),
    ("Paris.", "paris"),
    ("No. 5", "No 5"),
    ("Robert Douglas Benton", "Benton"),
    ("yes", "no"),
    ("Yes", "yes"),
    ("", ""),
    ("北京", "北京市"),
    ("The 1999 Italian Open", "1999 Italian Open"),
    ("Café", "Cafe"),
    ("noanswer", "noanswer"),
    ("a b", "b a"),
    ("café", "café"),
]


class ScorerTests(unittest.TestCase):
    def test_local_exact_match_rules(self):
        self.assertEqual(exact_match("Paris.", "paris"), 1.0)
        self.assertEqual(exact_match("yes", "no"), 0.0)
        self.assertEqual(exact_match("noanswer", "no"), 0.0)
        self.assertEqual(exact_match("Beijing", "Shanghai"), 0.0)
        # NFC 与 NFD 视为同一答案（与 DSPy 的 NFD 规范化一致）
        self.assertEqual(exact_match("café", "café"), 1.0)
        em, f1 = score("Paris", "Paris")
        self.assertEqual((em, f1), (1.0, 1.0))

    def test_matches_dspy_when_available(self):
        try:
            import dspy
        except ModuleNotFoundError:
            self.skipTest("未安装 dspy（research 可选依赖）")
        for prediction, gold in CASES:
            with self.subTest(prediction=prediction, gold=gold):
                expected = float(dspy.evaluate.answer_exact_match(
                    dspy.Example(answer=gold), dspy.Prediction(answer=prediction)))
                self.assertEqual(exact_match(prediction, gold), expected)


if __name__ == "__main__":
    unittest.main()
