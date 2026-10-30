"""结构化回答协议与确定性离线执行器；从文档抽取，不按题目ID查答案。"""
import json
from .data import citation, requested_slots

BASE_COMPONENTS = {'final_answer.predict':'按问题逐项回答，给出办理步骤。离线解释器：BASIC_SLOTS。',
                   'evidence_use_policy':'只用有效条款，逐事实引用。LATEST_VALID CITE_EACH NO_GUESS CONFLICT_ESCALATE'}
GOOD_PATCH = {'final_answer.predict':'先列出用户询问的所有事项，再逐项回答，不省略材料、审批或特殊情况。离线解释器：FULL_SLOTS。',
              'evidence_use_policy':'只用有效条款；冲突并列展示并升级人工；未知不猜；逐事实引用。LATEST_VALID CITE_EACH NO_GUESS CONFLICT_ESCALATE'}
BAD_PATCH = {'final_answer.predict':'尽可能缩短回答，取消逐事实引用。离线解释器：FULL_SLOTS。',
             'evidence_use_policy':'只用有效条款；冲突人工核实。LATEST_VALID NO_GUESS CONFLICT_ESCALATE'}


def parse_answer(raw):
    answer = json.loads(raw) if isinstance(raw,str) else raw
    if not isinstance(answer,dict) or set(answer) != {'facts','escalate','reason','conflicts'}:
        raise ValueError('回答必须包含且仅包含facts、escalate、reason、conflicts')
    if not isinstance(answer['facts'],list) or type(answer['escalate']) is not bool:
        raise ValueError('facts须为列表，escalate须为布尔值')
    if answer['reason'] not in {'','conflict','missing_evidence'} or not isinstance(answer['conflicts'],list):
        raise ValueError('未知不确定性类型')
    for row in answer['facts']:
        if not isinstance(row,dict) or set(row) != {'slot','value','citations'} or not all(isinstance(row[k],str) for k in ['slot','value']):
            raise ValueError('事实须包含slot、value、citations')
        if not isinstance(row['citations'],list) or any(not isinstance(x,str) for x in row['citations']):
            raise ValueError('事实引用格式错误')
    if len({r['slot'] for r in answer['facts']}) != len(answer['facts']):
        raise ValueError('事实槽位重复')
    if any(not isinstance(x,str) for x in answer['conflicts']):
        raise ValueError('冲突引用格式错误')
    return answer


def offline_answer(question, evidence, components):
    instructions = components['final_answer.predict']+' '+components['evidence_use_policy']
    active = [d for d in evidence if d['status']=='active']
    slots = requested_slots(question)
    conflicts = [citation(d) for d in active if d['scope']=='实践考试'] if 'practice_portal' in slots else []
    if conflicts and 'CONFLICT_ESCALATE' in instructions:
        return dict(facts=[],escalate=True,reason='conflict',conflicts=sorted(conflicts))
    if 'FULL_SLOTS' not in instructions and len(slots)>3:
        slots=slots[:3]
    facts=[]
    for slot in slots:
        sources=[d for d in active if slot in d['facts']]
        if sources:
            d=max(sources,key=lambda x:x['effective_date'])
            facts.append(dict(slot=slot,value=d['facts'][slot],citations=[citation(d)] if 'CITE_EACH' in instructions else []))
    unknown = not slots or any(w in question for w in ['一定','保证','免考'])
    return dict(facts=facts,escalate=unknown,reason='missing_evidence' if unknown else '',conflicts=[])
