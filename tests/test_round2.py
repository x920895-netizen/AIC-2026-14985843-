"""验证证据路由开关与本轮独立费用边界，无真实付费调用。"""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from dspy.utils import DummyLM
from hotpot_adapter import HotpotAdapter
from run_round2 import Round2Client


class Round2Tests(unittest.TestCase):
    def test_evidence_reaches_final_module_only_when_enabled(self):
        docs = ['Ada Example | Ada Middle Example is an author.']
        row = dict(id='fixture-new', split='train', question='Who is the author?', answer='Ada Middle Example',
                   supporting_facts={'title':['Ada Example']})
        for enabled in [False, True]:
            lm = DummyLM([{'reasoning':'r','summary':'Ada Example'}, {'reasoning':'r','query':'Ada'},
                          {'reasoning':'r','summary':'Ada Example'}, {'reasoning':'r','answer':'Ada Middle Example'}])
            client = SimpleNamespace(local=threading.local(), phase='fixture', spent=0)
            with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temp:
                adapter = HotpotAdapter(ROOT, SimpleNamespace(search=lambda query,k: docs), lm, client, temp,
                                        workers=1, direct_evidence=enabled)
                result = adapter.execute(row, adapter.seed_candidate())
                self.assertEqual(result['em'], 1)
                self.assertEqual(len(result['steps']), 4)
                inputs = result['steps'][-1]['inputs']
                self.assertEqual('evidence' in inputs, enabled)
                if enabled:
                    self.assertEqual(inputs['evidence'], docs)
                self.assertNotIn('answer', inputs)
                self.assertNotIn('supporting_facts', inputs)

    def test_round2_budget_excludes_round1_but_includes_resume(self):
        config = json.loads((ROOT / 'benchmark_config.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temp, patch.dict('os.environ', {'DASHSCOPE_API_KEY':'test-only'}):
            base = Path(temp)
            calibration = base / 'round1-calibration-2048'
            calibration.mkdir()
            (calibration / 'api_calls.jsonl').write_text(json.dumps(dict(phase='smoke',cost_cny=9,role='task'))+'\n')
            run = base / 'round2'
            run.mkdir()
            (run / 'api_calls.jsonl').write_text(json.dumps(dict(phase='smoke',cost_cny=0.2,role='task'))+'\n')
            client = Round2Client(config, run)
            self.assertEqual(client.spent, 0.2)


if __name__ == '__main__':
    unittest.main()
