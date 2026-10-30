"""先验证校园数据与评价器边界，再开展界面实现。"""
import copy
import json
import unittest
from campus_app.data import benchmark,documents,requested_slots
from campus_app.retrieval import retrieve
from campus_app.answers import BASE_COMPONENTS,GOOD_PATCH,offline_answer
from campus_app.evaluation import score_answer
from harness import GateSpec


class CampusProtocolTests(unittest.TestCase):
    def test_splits_and_metadata(self):
        tasks=benchmark();self.assertEqual(len(tasks),36)
        self.assertEqual(len({t['task_id'] for t in tasks}),36)
        for split in ['proposal','validation','holdout']:self.assertEqual(sum(t['split']==split for t in tasks),12)
        self.assertEqual(len(documents()),10)
        self.assertTrue(all(d['is_synthetic'] for d in documents()))

    def test_perfect_fact_and_citation_scoring(self):
        for label in benchmark():
            e=retrieve(label['question']);a=offline_answer(label['question'],e,GOOD_PATCH)
            with self.subTest(label=label['task_id']):self.assertEqual(score_answer(a,label,e)['strict'],1)

    def test_baseline_omission_is_observed(self):
        label=benchmark()[0];e=retrieve(label['question']);a=offline_answer(label['question'],e,BASE_COMPONENTS)
        self.assertEqual(score_answer(a,label,e)['strict'],0)

    def test_wrong_citation_unsupported_and_outdated(self):
        label=benchmark()[1];e=retrieve(label['question']);a=offline_answer(label['question'],e,GOOD_PATCH)
        a['facts'][0]['citations']=['CP02:第1条']
        self.assertIn('outdated_policy',score_answer(a,label,e)['errors'])
        a['facts'][0]['value']='无需申请，自动批准'
        self.assertGreater(score_answer(a,label,e)['unsupported_ratio'],0)

    def test_conflict_and_missing_evidence(self):
        for i in [7,8,9]:
            label=benchmark()[i];a=offline_answer(label['question'],retrieve(label['question']),GOOD_PATCH)
            self.assertTrue(a['escalate']);self.assertEqual(a['reason'],label['reason'])

    def test_protocol_names_are_distinct(self):
        a=GateSpec('a',('1',),{});b=GateSpec('a',('1',),{},evaluator_id='campus-policy-v1')
        self.assertNotEqual(a.evaluator_id,b.evaluator_id)
        with self.assertRaises(ValueError):GateSpec('a',('1',),{},evaluator_id='unknown')


if __name__=='__main__':unittest.main()
