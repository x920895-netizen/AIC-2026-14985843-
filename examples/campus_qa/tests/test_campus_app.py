"""真实执行应用功能、权限、持久化与API适配测试；网络发送均为隔离替身。"""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
REPO=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(REPO))
from fastapi.testclient import TestClient
from campus_app.api import create_app
from campus_app.service import CampusService,CONFIG_PATH
from campus_app.answers import GOOD_PATCH,BAD_PATCH,offline_answer
from campus_app.real_client import CampusModelClient,AppBudgetError
from campus_app.data import benchmark,documents
from harness.storage import StateConflict,FoundationStore
from harness import CandidateProposal,TargetKind


class CampusTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'state';self.service=CampusService(self.path)

    def good(self):return self.service.propose()['candidate']

    def test_full_demo_executes_accept_reject_restart_rollback(self):
        result=self.service.run_demo()
        self.assertTrue(result['accepted_evaluation']['passed']);self.assertFalse(result['rejected_evaluation']['passed'])
        self.assertTrue(result['restart_restored']);self.assertEqual(result['api_calls'],0)
        self.assertEqual(self.service.store.current()['version_id'],result['rollback_version'])

    def test_registration_diff_and_parent_traces(self):
        c=self.good();detail=self.service.store.read('campus_proposals',c['candidate_id'])
        self.assertIn('FULL_SLOTS',c['change']['diff'])
        for tid in detail['evidence_refs']:
            t=self.service.store.read('campus_traces',tid)
            self.assertEqual(t['parent_version_id'],c['parent_version_id']);self.assertEqual(t['split'],'proposal')

    def test_validation_pass_does_not_publish_or_accept(self):
        c=self.good();report=self.service.validate(c['candidate_id']);self.assertTrue(report['passed'])
        self.assertEqual(self.service.store.current()['version_id'],c['parent_version_id'])
        self.service.accept(c['candidate_id'])
        self.assertEqual(self.service.store.read('campus_meta','service')['published_version_id'],c['parent_version_id'])

    def test_net_gain_with_regression_is_still_rejected(self):
        c=self.good();r=self.service.validate(c['candidate_id'])
        bad=json.dumps(dict(facts=[],escalate=False,reason='',conflicts=[]))
        with self.service.store.transaction('test.net_gain') as conn:
            a=self.service.store.get(conn,'runs',r['run_ids'][0]);b=self.service.store.get(conn,'runs',r['run_ids'][1])
            a['outputs'][1]['answer']=bad;b['outputs'][2]['answer']=bad
            self.service.store.put(conn,'runs',a['run_id'],a);self.service.store.put(conn,'runs',b['run_id'],b)
            fresh=self.service.engine.validator.analyze(conn,c,*r['run_ids'])
        self.assertGreater(fresh['metrics']['candidate_em'],fresh['metrics']['parent_em'])
        self.assertEqual(fresh['metrics']['regressions'],1);self.assertFalse(fresh['passed'])
        self.assertIn('原正确题回归限制未通过',fresh['reasons'])

    def test_changed_gate_cannot_be_used(self):
        c=self.good();r=self.service.validate(c['candidate_id'])
        with self.service.store.transaction('test.gate') as conn:
            c=copy.deepcopy(c);c['validation_config']['min_em_gain']=0
            fresh=self.service.engine.validator.analyze(conn,c,*r['run_ids'])
        self.assertFalse(fresh['passed']);self.assertIn('门禁被修改，与初始化冻结规则不同',fresh['reasons'])

    def test_rollback_restores_both_components(self):
        c=self.good();before=self.service.components(c['parent_version_id']);self.service.validate(c['candidate_id']);self.service.accept(c['candidate_id'])
        self.service.rollback(c['parent_version_id'],'测试')
        self.assertEqual(self.service.components(c['parent_version_id']),before)

    def test_restart_in_another_process(self):
        c=self.good();self.service.validate(c['candidate_id']);self.service.accept(c['candidate_id'])
        command='from campus_app.service import CampusService;import sys;print(CampusService(sys.argv[1]).store.current()["version_id"])'
        env=dict(os.environ);env['PYTHONPATH']=str(REPO)+os.pathsep+env.get('PYTHONPATH','')
        run=subprocess.run([sys.executable,'-B','-X','utf8','-c',command,str(self.path)],cwd=ROOT,
            env=env,capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(run.returncode,0,run.stderr);self.assertEqual(run.stdout.strip(),c['version_id'])

    def test_recompute_ignores_cached_mean(self):
        c=self.good();report=self.service.validate(c['candidate_id'])
        with self.service.store.transaction('test.metric') as conn:
            stored=self.service.store.get(conn,'evaluations',report['evaluation_id']);stored['metrics']['candidate_em']=0
            self.service.store.put(conn,'evaluations',report['evaluation_id'],stored)
        with self.assertRaises(StateConflict):self.service.accept(c['candidate_id'])

    def test_tampered_output_recomputed_before_acceptance(self):
        c=self.good();report=self.service.validate(c['candidate_id'])
        with self.service.store.transaction('test.output') as conn:
            r=self.service.store.get(conn,'runs',report['run_ids'][1]);r['outputs'][0]['answer']=json.dumps(dict(facts=[],escalate=False,reason='',conflicts=[]))
            self.service.store.put(conn,'runs',r['run_id'],r)
        with self.assertRaises(StateConflict):self.service.accept(c['candidate_id'])

    def test_duplicate_candidate_rejected(self):
        self.good()
        with self.assertRaises(StateConflict):self.good()

    def test_stale_parent_cannot_be_accepted(self):
        a=self.good();b=self.service.propose(fixture='regression')['candidate'];self.service.validate(a['candidate_id']);self.service.accept(a['candidate_id'])
        report=self.service.validate(b['candidate_id']);self.assertFalse(report['passed'])

    def test_validation_cannot_change_mode(self):
        c=self.good()
        with self.assertRaises(ValueError):self.service.validate(c['candidate_id'],'real',True)

    def test_runtime_inputs_have_no_labels(self):
        seen=[];old=self.service.execute
        def spy(question,vid,mode,confirmed=False):
            seen.append(question);return old(question,vid,mode,confirmed)
        self.service.execute=spy;self.good()
        self.assertEqual(seen,[t['question'] for t in benchmark() if t['split']=='proposal'])
        self.assertFalse(any('holdout-' in t for t in seen))

    def test_unreviewed_feedback_not_gold(self):
        a=self.service.chat('缓考什么时候申请？');feedback=self.service.feedback(a['run_id'],'遗漏条件')
        self.assertFalse(feedback['gold_eligible']);self.assertEqual(feedback['status'],'unreviewed')
        self.assertEqual(self.service.store.list('campus_traces'),[])

    def test_interactive_privacy_default(self):
        a=self.service.chat('缓考什么时候申请？');r=self.service.store.read('campus_chats',a['run_id'])
        self.assertNotIn('question',r);self.assertNotIn('result',r)

    def test_holdout_requires_freeze_and_no_repeat(self):
        with self.assertRaises(StateConflict):self.service.holdout()
        self.service.freeze();result=self.service.holdout();self.assertEqual(result['n'],12)
        with self.assertRaises(StateConflict):self.service.holdout()
        self.good();self.assertFalse(self.service.store.read('campus_meta','holdout')['independent'])

    def test_feedback_cannot_introduce_validation_references(self):
        p=self.service.propose()['proposal'];p=copy.deepcopy(p)
        for k in ['mode','source','cost','candidate_id','diagnostic_run_id']:p.pop(k)
        p['evidence_refs']=['validation-01']
        with self.assertRaises(ValueError):self.service.validate_patch(p,p['parent_version_id'],self.service.store.list('campus_traces'))

    def test_patch_cannot_add_evaluator_or_gold(self):
        p=self.service.propose()['proposal'];p=copy.deepcopy(p)
        for k in ['mode','source','cost','candidate_id','diagnostic_run_id']:p.pop(k)
        p['evaluator_id']='other'
        with self.assertRaises(ValueError):self.service.validate_patch(p,p['parent_version_id'],self.service.store.list('campus_traces'))
        p.pop('evaluator_id');p['prompt_patch']='答案是考试前2个工作日'
        with self.assertRaises(ValueError):self.service.validate_patch(p,p['parent_version_id'],self.service.store.list('campus_traces'))

    def test_frozen_environment_cannot_change(self):
        self.service.corpus[0]['content']='变更'
        with self.assertRaises(StateConflict):self.service.chat('缓考什么时候申请？')

    def test_missing_duplicate_or_failed_outputs_rejected(self):
        c=self.good();report=self.service.validate(c['candidate_id'])
        for mutation in ['missing','duplicate','failed']:
            with self.subTest(mutation=mutation):
                with self.service.store.transaction('test.mutation') as conn:
                    r=self.service.store.get(conn,'runs',report['run_ids'][1]);original=copy.deepcopy(r)
                    if mutation=='missing':r['outputs'].pop()
                    elif mutation=='duplicate':r['outputs'][0]=r['outputs'][1]
                    else:r['status']='failed'
                    self.service.store.put(conn,'runs',r['run_id'],r)
                    fresh=self.service.engine.validator.analyze(conn,c,*report['run_ids']);self.assertFalse(fresh['passed'])
                    self.service.store.put(conn,'runs',original['run_id'],original)

    def test_unauthorized_rollback_and_publish(self):
        c=self.good()
        with self.assertRaises(StateConflict):self.service.rollback(c['version_id'],'不能回滚候选')
        with self.assertRaises(ValueError):self.service.publish(c['parent_version_id'],False)

    def test_no_gain_is_inconclusive(self):
        c=self.good();ref=self.service.store.write_artifact('prompt',{'components':dict(GOOD_PATCH,**{'final_answer.predict':BASE_TEXT})})
        parent=self.service.store.current()['version_id']
        c=self.service.engine.register(CandidateProposal(parent,TargetKind.PROMPT,ref,self.service.spec(),'同效果改写'))
        self.service.save('campus_proposals',c['candidate_id'],dict(mode='offline'))
        r=self.service.validate(c['candidate_id']);self.assertFalse(r['passed']);self.assertEqual(r['verdict'],'inconclusive')

    def test_api_input_and_buttons_mutate_backend(self):
        client=TestClient(create_app(self.path))
        self.assertEqual(client.get('/').status_code,200)
        self.assertEqual(client.get('/static/app.js').status_code,200)
        self.assertEqual(client.get('/api/health').json()['default_mode'],'offline')
        self.assertEqual(client.post('/api/chat',json={'question':''}).status_code,422)
        chat=client.post('/api/chat',json={'question':'缓考什么时候申请？'}).json()
        self.assertEqual(client.post('/api/feedback',json=dict(run_id=chat['run_id'],category='回答不准确')).status_code,200)
        c=client.post('/api/evolution/propose',json={}).json()['candidate']
        r=client.post('/api/evolution/validate',json={'candidate_id':c['candidate_id']});self.assertTrue(r.json()['passed'])
        self.assertEqual(client.post('/api/evolution/accept',json={'candidate_id':c['candidate_id']}).status_code,200)
        self.assertEqual(client.get('/api/evolution/state').json()['current']['version_id'],c['version_id'])
        self.assertEqual(client.post('/api/evolution/rollback',json=dict(version_id=c['parent_version_id'],reason='测试')).status_code,200)

    def test_cross_origin_blocked_and_no_background_calls(self):
        send=lambda *a:(_ for _ in ()).throw(AssertionError('禁止隐式发送'))
        client=TestClient(create_app(self.path,allow_real=True,send=send))
        self.assertEqual(client.get('/api/health').status_code,200);self.assertEqual(client.get('/api/evolution/state').status_code,200)
        self.assertEqual(client.post('/api/chat',json={'question':'测试'},headers={'Origin':'https://evil.invalid'}).status_code,403)

    def test_no_key_or_unconfirmed_real_request(self):
        c=TestClient(create_app(self.path,allow_real=True))
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':''}):
            self.assertEqual(c.post('/api/chat',json=dict(question='缓考期限',mode='real',confirm_paid=True)).status_code,400)
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'fake-test-key'}):
            self.assertEqual(c.post('/api/chat',json=dict(question='缓考期限',mode='real')).status_code,400)
        self.assertEqual(self.service.store.list('app_api_calls'),[])

    def test_budget_cap_and_unknown_send_stop(self):
        config=copy.deepcopy(self.service.config);config['budget_cny']=0
        client=CampusModelClient(self.service.store,config,enabled=True,send=lambda *a:None)
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'fake-test-key'}):
            incomplete=copy.deepcopy(config);incomplete.pop('task_model')
            with self.assertRaisesRegex(ValueError,'配置缺少'):
                CampusModelClient(self.service.store,incomplete,enabled=True).validate_config(True)
            with self.assertRaises(AppBudgetError):client.call('task',[{'role':'user','content':'test'}],confirmed=True)
            config['budget_cny']=2
            client.send=lambda *a:(_ for _ in ()).throw(TimeoutError('测试超时'))
            with self.assertRaises(TimeoutError):client.call('task',[{'role':'user','content':'test'}],confirmed=True)
            with self.assertRaises(AppBudgetError):client.call('task',[{'role':'user','content':'test'}],confirmed=True)
        records=self.service.store.list('app_api_calls');self.assertEqual(len(records),1);self.assertEqual(records[0]['status'],'unknown')

    def test_mocked_live_loop_uses_real_adapter_not_fixtures(self):
        sent=[]
        def send(url,key,payload,timeout):
            sent.append(payload);data=json.loads(payload['messages'][1]['content'])
            if 'failures' in data:
                out=dict(parent_version_id=data['parent_version_id'],hypothesis='完整列出询问事项',
                    evidence_refs=[x['trace_id'] for x in data['failures']],prompt_patch=GOOD_PATCH['final_answer.predict'],
                    evidence_policy_patch=GOOD_PATCH['evidence_use_policy'],expected_benefit='减少遗漏',risks=['可能产生冗长回答'],
                    applicability='缓考咨询',counterexamples_to_check=['未知与冲突'])
                self.assertTrue(all(t['split']=='proposal' for t in data['failures']))
            else:
                components=GOOD_PATCH if 'FULL_SLOTS' in payload['messages'][0]['content'] else self.service.components(self.service.store.current()['version_id'])
                out=offline_answer(data['question'],data['evidence'],components)
                self.assertNotIn('expected_facts',data)
            return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(out,ensure_ascii=False)}}],
                    'usage':{'prompt_tokens':100,'completion_tokens':50}}
        live=CampusService(self.path,allow_real=True,send=send)
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'fake-test-key'}):
            c=live.propose('real',True)['candidate'];r=live.validate(c['candidate_id'],'real',True)
            self.assertTrue(r['passed'],r['reasons']);live.accept(c['candidate_id'])
        self.assertEqual(len(sent),37);self.assertEqual(len(live.store.list('app_api_calls')),37)
        self.assertTrue(all('messages' not in x for x in live.store.list('app_api_calls')))

    def test_real_invalid_proposal_has_no_fixture_fallback(self):
        # 对真实反思输出的schema校验独立于模型质量。
        def send(url,key,payload,timeout):
            d=json.loads(payload['messages'][1]['content'])
            a={} if 'failures' in d else offline_answer(d['question'],d['evidence'],self.service.components(self.service.store.current()['version_id']))
            return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(a)}}], 'usage':{'prompt_tokens':10,'completion_tokens':10}}
        live=CampusService(self.path,allow_real=True,send=send)
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'fake-test-key'}):
            with self.assertRaises(ValueError):live.propose('real',True)
        self.assertEqual(live.store.list('candidates'),[]);self.assertEqual(len(live.store.list('campus_proposal_failures')),1)


BASE_TEXT='按问题逐项回答，给出办理步骤。离线解释器：BASIC_SLOTS。'
if __name__=='__main__':unittest.main()
