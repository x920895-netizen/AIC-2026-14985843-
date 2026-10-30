"""完全合成的校园制度及独立标注任务；无真实高校信息。"""
from copy import deepcopy

DISCLAIMER = '演示环境使用模拟校园政策，不代表任何真实高校的现行规定。'
SCHEMA = 'campus-policy-v1'


def document(number, title, content, facts, *, status='active', version='2026.2', date='2026-09-01', scope='理论考试'):
    return dict(doc_id=f'CP{number:02d}', title=title, clause_id='第1条', content=content,
                facts=facts, version=version, effective_date=date, status=status,
                source_type='synthetic_campus_policy', is_synthetic=True, scope=scope)


DOCUMENTS = [
    document(1, '考试管理办法·缓考条件', '理论考试因身体原因或不可抗力无法按时参加，可申请缓考；申请不等于批准。',
             {'condition':'因身体原因或不可抗力无法按时参加理论考试，可申请缓考，申请不等于批准。'}),
    document(2, '缓考申请期限·旧版', '旧版要求考试前5个自然日申请；本条已被2026.2替代。',
             {'deadline':'考试前5个自然日申请。'},status='superseded',version='2025.1',date='2025-09-01'),
    document(3, '缓考申请期限·现行版', '普通缓考应不晚于考试前2个工作日提出；考试当天突发情况适用特殊流程。',
             {'deadline':'普通缓考应不晚于考试前2个工作日提出。'}),
    document(4, '缓考材料清单', '身体原因申请缓考需要缓考申请表和医疗机构出具的相关证明。',
             {'materials':'缓考申请表和医疗机构出具的相关证明。'}),
    document(5, '理论考试线上申请流程', '理论考试由教务服务系统的考试服务—缓考申请入口提交并上传材料。',
             {'portal':'教务服务系统→考试服务→缓考申请，上传材料后提交。'}),
    document(6, '缓考审批职责', '任课教师核实课程和考试信息，学生所在学院审批，教务部门备案。',
             {'approver':'任课教师核实课程和考试信息，学生所在学院审批，教务部门备案。'}),
    document(7, '逾期及突发情况处理', '错过一般期限先联系所在学院教务办公室说明原因并申请特殊处理，不保证批准；考试当天突发身体原因应及时联系学院并补交证明。',
             {'late':'错过一般期限先联系所在学院教务办公室说明原因，申请特殊处理，不保证批准。',
              'emergency':'考试当天突发身体原因，应及时联系所在学院教务办公室并补交证明，由学院核实处理。'}),
    document(8, '人工咨询与资料不足处理', '材料不足、审批状态不明或条款冲突时，联系所在学院教务办公室核实；演示资料不提供真实电话或审批结果。',
             {'channel':'联系所在学院教务办公室；演示资料不提供真实电话。'}),
    document(9, '实践考试提交补充说明A', '实践考试的缓考申请由学生向学院窗口提交纸质材料。与同日说明B存在未解决冲突。',
             {'practice_portal':'向学院窗口提交纸质材料。'}, scope='实践考试'),
    document(10, '实践考试提交补充说明B', '实践考试的缓考申请仅通过教务服务系统提交。与同日说明A存在未解决冲突。',
             {'practice_portal':'仅通过教务服务系统提交。'}, scope='实践考试'),
]

SLOT_NAMES = dict(condition='适用条件', deadline='申请期限', materials='材料清单', portal='提交入口',
                  approver='审批职责', late='逾期处理', emergency='突发情况', channel='人工咨询', practice_portal='实践考试入口')


def citation(doc):
    return doc['doc_id'] + ':' + doc['clause_id']


def requested_slots(question):
    # 只读取问题文本，不接收任务ID或评分标签；检索与该意图解析均固定。
    if '实践' in question:
        return ['practice_portal']
    slots = []
    for words, slot in [(['符合','条件','身体原因','能申请'], 'condition'),
                        (['什么时候','期限','几天','日期','旧版','5个自然日','时间'], 'deadline'),
                        (['材料','证明'], 'materials'), (['哪里','入口','提交','流程'], 'portal'),
                        (['谁审批','审批人','审批职责','由谁'], 'approver'),
                        (['错过','逾期'], 'late'), (['当天','突发'], 'emergency'),
                        (['联系','人工','电话'], 'channel')]:
        if any(w in question for w in words):
            slots.append(slot)
    return slots


def documents():
    return deepcopy(DOCUMENTS)


def benchmark():
    # 金标准单独构建，仅由评价器与受控proposal反思读取。
    variants = [
        ('我因身体原因无法参加下周理论考试，缓考什么时候申请，需要哪些材料、在哪里提交、由谁审批？错过时间怎么办？',
         ['condition','deadline','materials','portal','approver','late'], False, ''),
        ('理论考试缓考需要提前几天申请？', ['deadline'], False, ''),
        ('身体原因缓考要提交哪些材料和证明？', ['condition','materials','portal'], False, ''),
        ('理论考试缓考在哪里提交，线上入口是什么？', ['portal'], False, ''),
        ('缓考由谁审批，审批职责如何划分？', ['approver'], False, ''),
        ('错过一般申请期限，逾期缓考怎么办？', ['deadline','late'], False, ''),
        ('旧版要求5个自然日，现在理论考试缓考申请期限是什么？', ['deadline'], False, ''),
        ('实践考试缓考到底在哪里提交，两个补充说明冲突怎么办？', [], True, 'conflict'),
        ('体育免考一定有免考名额吗？', [], True, 'missing_evidence'),
        ('提交缓考申请后一定能通过吗，能保证批准吗？', ['portal'], True, 'missing_evidence'),
        ('考试当天突发身体原因，缓考材料和特殊处理怎么办？', ['condition','materials','emergency'], False, ''),
        ('缓考资料不足想人工核实，联系谁，有真实电话吗？', ['channel'], False, ''),
    ]
    valid = {slot:(value,citation(d)) for d in DOCUMENTS if d['status']=='active' and d['scope']=='理论考试'
             for slot,value in d['facts'].items()}
    tasks = []
    for split, prefix in [('proposal','请帮我梳理：'),('validation','请按办事要求说明：'),('holdout','请依据演示制度核对：')]:
        for i,(question,slots,escalate,reason) in enumerate(variants,1):
            tasks.append(dict(task_id=f'{split}-{i:02d}', question=prefix+question, split=split, is_synthetic=True,
                expected_facts={slot:valid[slot][0] for slot in slots},
                required_citations={slot:[valid[slot][1]] for slot in slots},
                can_answer=not escalate, escalate=escalate, reason=reason,
                conflict_refs=['CP09:第1条','CP10:第1条'] if reason=='conflict' else [],
                errors_are_annotations=True))
    return tasks
