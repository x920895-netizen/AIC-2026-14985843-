"""校园评价适配器；标签独立保管，由持久化输出重算事实与配对指标。"""
import json
from harness.contracts import GateSpec, content_hash, now
from .answers import parse_answer
from .data import citation


def score_answer(raw, label, evidence):
    answer=parse_answer(raw)
    support={citation(d):d for d in evidence if d['status']=='active'}
    facts=answer['facts']; expected=label['expected_facts']
    correct=sum(r['slot'] in expected and r['value']==expected[r['slot']] for r in facts)
    precision=correct/len(facts) if facts else (1.0 if not expected else 0.0)
    recall=correct/len(expected) if expected else (1.0 if not facts else 0.0)
    f1=2*precision*recall/(precision+recall) if precision+recall else 0.0
    supported=[]; cited=[]; errors=[]
    for r in facts:
        sources=[d for d in support.values() if d['facts'].get(r['slot'])==r['value']]
        supported.append(bool(sources))
        cited.append(bool(r['citations']) and all(ref in support and support[ref]['facts'].get(r['slot'])==r['value'] for ref in r['citations']))
        if not supported[-1]: errors.append('unsupported_claim')
        if not cited[-1]: errors.append('citation_mismatch')
        if any(ref.startswith('CP02:') for ref in r['citations']): errors.append('outdated_policy')
    coverage=sum(cited)/len(facts) if facts else 1.0
    unsupported=1-sum(supported)/len(facts) if facts else 0.0
    needed_refs=all(any(r['slot']==slot and r['value']==expected[slot] and set(refs)&set(r['citations']) for r in facts)
                    for slot,refs in label['required_citations'].items())
    safe=(answer['escalate']==label['escalate'] and answer['reason']==label['reason'] and
          set(answer['conflicts'])==set(label['conflict_refs']))
    if not safe: errors.append('conflict_not_detected' if label['reason']=='conflict' else 'unsafe_overconfidence')
    missing=set(expected)-{r['slot'] for r in facts if r['slot'] in expected and r['value']==expected[r['slot']]}
    for slot in missing:
        errors.append({'deadline':'wrong_deadline','materials':'missing_material','portal':'wrong_process','approver':'wrong_approver'}.get(slot,'missing_fact'))
    strict=float(f1==1 and needed_refs and coverage==1 and unsupported==0 and safe)
    return dict(strict=strict,f1=f1,precision=precision,recall=recall,citation_coverage=coverage,
                unsupported_ratio=unsupported,safety_ok=safe,errors=sorted(set(errors)))


class CampusValidator:
    def __init__(self,store): self.store=store

    def analyze(self,conn,candidate,parent_run_id,child_run_id):
        spec=GateSpec.from_dict(candidate['validation_config'])
        if spec.evaluator_id!='campus-policy-v1': raise ValueError('校园评价器不接受其他协议')
        report=dict(candidate_id=candidate['candidate_id'],mode='offline',created_at=now(),
                    run_ids=[parent_run_id,child_run_id],task_ids=list(spec.task_ids),
                    metrics={},reasons=[],passed=False,verdict='rejected',gates=[])
        runs=[self.store.get(conn,'runs',rid) for rid in report['run_ids']]
        dataset=self.store.get(conn,'datasets',spec.dataset_id)
        reasons=report['reasons']; report['mode']=runs[0]['mode']
        parent_version=self.store.get_version(conn,candidate['parent_version_id'])
        if spec.to_dict()!=parent_version['config_snapshot']['gate_snapshot']:reasons.append('门禁被修改，与初始化冻结规则不同')
        expected=list(spec.task_ids)
        if self.store.current_id(conn)!=candidate['parent_version_id']: reasons.append('候选父版本已过期')
        for run,vid in zip(runs,[candidate['parent_version_id'],candidate['version_id']]):
            version=self.store.get_version(conn,vid)
            if run['version_id']!=vid or run['status']!='completed': reasons.append('运行版本不匹配或未完整成功')
            if run['condition_hash']!=content_hash(spec.to_dict()) or run['validation_config']!=spec.to_dict(): reasons.append('冻结条件不匹配')
            if [r['task_id'] for r in run['outputs']]!=expected: reasons.append('任务缺失、重复或顺序改变')
            if run['environment_hash']!=version['config_snapshot']['environment_hash']: reasons.append('检索、模型或数据版本改变')
            if run['scope_id']!=runs[0]['scope_id'] or run['mode']!=runs[0]['mode']: reasons.append('父子预算作用域或模式不同')
            if run['version_hash']!=version['content_hash']: reasons.append('执行后策略版本改变')
            if any(r['status']!='completed' for r in run['outputs']): reasons.append('存在失败输出')
            if run['mode']=='offline' and (run['api_calls']!=0 or run['actual_api_cost_cny']!=0): reasons.append('离线模式发生真实调用')
        if runs[1]['candidate_id']!=candidate['candidate_id']: reasons.append('候选运行身份不符')
        if runs[0]['mode'] not in {'offline','real'}: reasons.append('未知执行模式')
        ledger=[self.store.get(conn,'app_api_calls',r[0]) for r in conn.execute("SELECT id FROM documents WHERE collection='app_api_calls'")]
        if any(r['status'] in {'sending','unknown','overrun'} for r in ledger):reasons.append('真实调用存在未决状态或预留超限')
        if runs[0]['mode']=='real':
            for run in runs:
                attempts=[]
                for row in run['outputs']:
                    if not row.get('attempt_id'):reasons.append('真实输出缺少调用账本引用');continue
                    attempt=self.store.get(conn,'app_api_calls',row['attempt_id']);attempts.append(attempt)
                    if attempt['status']!='settled' or abs(attempt['accounted_cny']-row['cost_cny'])>1e-12:reasons.append('实际用量账本不匹配')
                if len({a['attempt_id'] for a in attempts})!=len(run['outputs']):reasons.append('调用账本引用缺失或重复')
                if run['api_calls']!=len(attempts) or abs(run['actual_api_cost_cny']-sum(a['accounted_cny'] for a in attempts))>1e-12:reasons.append('运行费用不匹配')
        if reasons:return report
        rows=[]
        for old,new in zip(runs[0]['outputs'],runs[1]['outputs']):
            if old['evidence']!=new['evidence']: reasons.append('父子证据不一致');return report
            label=json.loads(dataset['labels'][old['task_id']])
            a=score_answer(old['answer'],label,old['evidence']); b=score_answer(new['answer'],label,new['evidence'])
            rows.append(dict(task_id=old['task_id'],parent=a,candidate=b))
        mean=lambda side,key:sum(r[side][key] for r in rows)/len(rows)
        old_correct=sum(r['parent']['strict'] for r in rows)
        fixes=sum(not r['parent']['strict'] and bool(r['candidate']['strict']) for r in rows)
        regress=sum(bool(r['parent']['strict']) and not r['candidate']['strict'] for r in rows)
        rate=regress/old_correct if old_correct else None
        report['metrics']=dict(n=len(rows),parent_em=mean('parent','strict'),candidate_em=mean('candidate','strict'),
            parent_f1=mean('parent','f1'),candidate_f1=mean('candidate','f1'),fixes=fixes,regressions=regress,
            regression_rate=rate,parent_citation_coverage=mean('parent','citation_coverage'),candidate_citation_coverage=mean('candidate','citation_coverage'),
            parent_unsupported_ratio=mean('parent','unsupported_ratio'),candidate_unsupported_ratio=mean('candidate','unsupported_ratio'),
            parent_cost_cny=runs[0]['actual_api_cost_cny'],candidate_cost_cny=runs[1]['actual_api_cost_cny'],
            api_calls=sum(r['api_calls'] for r in runs),seconds=sum(r['seconds'] for r in runs),per_task=rows)
        m=report['metrics']
        checks=[('严格正确率增益',m['candidate_em']-m['parent_em']+1e-12>=spec.min_em_gain),
            ('事实F1下降限制',m['candidate_f1']+spec.max_f1_drop+1e-12>=m['parent_f1']),
            ('原正确题回归限制',rate is None or rate<=spec.max_regression_rate+1e-12),
            ('引用覆盖不下降',m['candidate_citation_coverage']+1e-12>=m['parent_citation_coverage']),
            ('无依据事实不增加',m['candidate_unsupported_ratio']<=m['parent_unsupported_ratio']+1e-12),
            ('安全与冲突不退化',all(r['candidate']['safety_ok'] for r in rows)),
            ('成本总额',sum(r['actual_api_cost_cny'] for r in runs)<=spec.max_total_cost_cny+1e-12),
            ('成本比',runs[1]['actual_api_cost_cny']<=runs[0]['actual_api_cost_cny']*spec.max_cost_ratio+1e-12)]
        report['gates']=[dict(name=name,passed=bool(ok)) for name,ok in checks]
        report['reasons']=[name+'未通过' for name,ok in checks if not ok]
        report['passed']=all(ok for _,ok in checks)
        report['verdict']='eligible' if report['passed'] else ('inconclusive' if len(report['reasons'])==1 and not checks[0][1] else 'rejected')
        return report
