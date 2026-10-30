"""离线整理首轮逐题证据；在任务实现修改前保存基线快照。

需本地原始账本（runs/round1/rollouts.jsonl 与 data/splits.json）。
"""
import json
import shutil
from collections import Counter
from pathlib import Path

try:
    from ._ledger import require_files
except ImportError:  # 以脚本方式直接运行
    from _ledger import require_files

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / 'runs/round2'


def load(path):
    return json.loads((ROOT / path).read_text(encoding='utf-8'))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def diagnosis(r):
    if r['em']:
        return ['当前正确'], '本次输出 EM=1；单次轨迹无法证明重复运行稳定性', '高'
    if r['id'] == '5a7c9b6955429935c91b525d':
        return ['答案综合/候选歧义', '可能的数据歧义'], '已命中 Brandenburg Cup，但摘要还列出其他同赞助赛事；问题描述可能不唯一，不能只归因于模型', '中'
    if r['id'] == '5a7de2065542995f4f4022f2':
        return ['检索缺证据', '关系连接错误'], '仅命中一半支持文档；最终日期与参考不同，需核对同校四分卫的桥接关系，完整证据见逐题记录', '中'
    if r['hop2_support_recall'] == 0:
        return ['检索缺证据', '无依据猜测'], '两跳均未命中任何标注支持标题；摘要/最终答案仍给出实体，检索缺失是已知事实，缺失的因果贡献尚未消融', '高'
    if r['split'] == 'test' and r['id'] == '5ae07acc55429945ae95935c':
        return ['检索缺证据', '无依据猜测'], '缺少完整支持文档，最终地理关系使用内部知识推断', '中'
    if r['f1'] > 0:
        return ['答案表述/粒度不匹配', *(['检索缺证据'] if r['hop2_support_recall'] < 1 else [])], '预测与参考有词元交集但 EM=0；需结合正文全名、标题、摘要判断；不能视为评分器故障', '高'
    return ['证据不足/综合错误待区分', *(['检索缺证据'] if r['hop2_support_recall'] < 1 else [])], '标注标题召回不能证明答案句已经检索；保留完整摘要和检索内容供核对', '中'


def main():
    require_files(ROOT / "data/splits.json", ROOT / "runs/round1/baseline_test.json",
                  ROOT / "runs/round1/optimized_test.json", ROOT / "runs/round1/best_candidate.json",
                  ROOT / "runs/round1/rollouts.jsonl")
    RUN.mkdir(parents=True, exist_ok=True)
    snap = RUN / 'before'
    if not snap.exists():
        snap.mkdir()
        for name in ['scripts', 'tests']:
            shutil.copytree(ROOT / name, snap / name, ignore=shutil.ignore_patterns('__pycache__'))
        for name in ['benchmark_config.json', 'requirements.txt']:
            shutil.copy2(ROOT / name, snap / name)
        for name in ['seed_candidate.json', 'best_candidate.json', 'config.json', 'environment.json']:
            shutil.copy2(ROOT / 'runs/round1' / name, snap / name)
    splits = load('data/splits.json')
    lookup = {r['id']: r for rows in splits.values() for r in rows}
    baseline = load('runs/round1/baseline_test.json')
    optimized = load('runs/round1/optimized_test.json')
    best = load('runs/round1/best_candidate.json')
    records = [json.loads(l) for l in (ROOT / 'runs/round1/rollouts.jsonl').read_text(encoding='utf-8').splitlines()]
    val = {r['id']: r for r in records if r['split'] == 'val' and r['candidate'] == best}
    table = []
    for a, b in zip(baseline, optimized):
        assert a['id'] == b['id']
        cat, evidence, confidence = diagnosis(b)
        group = 'D' if a['em'] and b['em'] else 'B' if a['em'] else 'A' if b['em'] else 'C'
        table.append(dict(question_id=b['id'], question_type=lookup[b['id']]['type'], question=lookup[b['id']]['question'],
            group=group, initial_answer=a['answer'], optimized_answer=b['answer'], reference_answer=b['gold'],
            initial_em=a['em'], optimized_em=b['em'], initial_f1=a['f1'], optimized_f1=b['f1'],
            execution_trace={'initial': a.get('steps', 'unavailable'), 'optimized': b.get('steps', 'unavailable')},
            retrieved_evidence={'initial': a['prediction'], 'optimized': b['prediction']},
            failure_category=cat, evidence_for_diagnosis=evidence, confidence=confidence,
            proposed_fix='将原始检索证据直接路由到最终答案模块，允许核对被摘要压缩的姓名；不改评分或参考答案' if '答案表述/粒度不匹配' in cat else '保留现状，后续按类别单独消融'))
    save(RUN / 'round1_test_comparison.json', table)
    save(RUN / 'round1_val_evidence.json', [{'row': lookup[r['id']], 'result': r, 'diagnosis': diagnosis(r)} for r in val.values()])
    counts = Counter(t['group'] for t in table)
    cats = Counter(c for t in table if not t['optimized_em'] for c in t['failure_category'])
    lines = ['# Round 2 错误归因（修改前）', '',
        '检查范围：项目根目录 `git status --short` 返回“非 Git 仓库”；没有提交或破坏性操作。修改前源码、测试、配置与候选已保存到 [[runs/round2/before|基线快照]]。已读取首轮报告、数据生成/检索/适配/客户端/运行代码、7 项实际测试代码及所有首轮逐题轨迹。上游核心保持原样。', '',
        '轨迹记录包含四模块输入/输出、两跳全文检索结果与参考答案，字段没有缺失；模型内部真实思维过程和重复采样稳定性为 unavailable。原始 90 个 ID 互不重叠。首轮当前账本有42次失败（超时1、限流3、连接37、截断1），加上校准账本共44次；历史 failure.json 时间早于 completed 汇总，属于已恢复的历史中断。', '',
        f'逐题核实：A={counts["A"]}、B={counts["B"]}、C={counts["C"]}、D={counts["D"]}。新增答对4题、退化0题与报告一致。D类仅能证明本次正确，不能证明稳定。', '',
        '## 30 题对照', '', '| ID | 类型 | 类 | 参考 | 初始预测 | R1预测 | EM 初/R1 | F1 初/R1 | 归因（主因在前） |', '|---|---|---|---|---|---|---|---|---|']
    for t in table:
        escape = lambda x: str(x).replace('|', '\\|').replace('\n', ' ')
        lines.append('| ' + ' | '.join(map(escape, [t['question_id'], t['question_type'], t['group'], t['reference_answer'], t['initial_answer'], t['optimized_answer'], f"{t['initial_em']:.0f}/{t['optimized_em']:.0f}", f"{t['initial_f1']:.3f}/{t['optimized_f1']:.3f}", '；'.join(t['failure_category'])])) + ' |')
    lines += ['', '完整逐题执行轨迹、检索证据、诊断依据、建议和置信度见 [[runs/round2/round1_test_comparison.json|逐题对照数据]]；验证集证据见 [[runs/round2/round1_val_evidence.json|验证轨迹]]。', '', '## 失效模式与证据', '']
    lines += [f'- {cat}：{n}/7 个 R1测试错误（{n/7:.1%}）；占全部30题 {n/30:.1%}。类别可以重叠。' for cat, n in cats.items()]
    lines += ['',
        '已确认：验证集7个错误中6个存在明显答案表述差异（航空行业限定词、三个人名短式、一个人名长式、航运公司后缀）；另1个涉及日期/关系连接。正文包含 Oswald Ernald Mosley、Jonathan Allen Lethem、Declan Benedict McKenna，而最终分别使用标题短名。John Patrick Blake 对参考 John Blake 的反例表明“总取全名”也会损失分数。', '',
        '测试中 Caleb Shomo 与 Luis de Jesús Rodríguez 两题：原始正文包含参考全名，摘要缩短或同时列出长短名，最终遵循 R1 的标题优先规则而失配。其余主要证据：三题支持标题召回为0（滑冰、橄榄球、Smash）；Aeolian Singers 仅找到所在郡，北邻关系由内部知识猜测；Brandenburg Cup 案例存在多个赞助杯赛，数据问题可能性需保留。', '',
        'A类有效变化：退休日期题与电影演员题的支持召回均由0.5升到1；国籍比较题将解释性句子缩成 No；联赛题在召回同为1时改善综合。召回与得分一起变化属于相关证据，不能将全部增益归于某条Prompt。B类为空。', '',
        'R1 最终模块要求核对原始文档，却只接收两份摘要；并明确要求使用 `|` 前标题。接口与指令不一致由源码直接确认。它解释部分全名丢失，有验证集独立证据。模型不稳定目前未重复采样；缓存污染/评分实现错误未在记录中发现，不能把 EM 对别名的严格扣分当作实现缺陷。', '',
        '## 优先顺序与本轮选择', '',
        '1. 原始证据路由到最终模块：验证集3个短名错误与源码接口不一致直接支持，成本低；可能因干扰证据增加或全名偏好伤害原正确答案。',
        '2. 查询改进/扩展召回：测试3个完全未命中，潜在影响较大，但验证集主导错误来自表述；留下一轮单独研究。',
        '3. 评分/答案别名处理：保持原评分，无法通用确定期望粒度，不实施字符串删词或答案映射。', '',
        '本轮仅实施可开关的“最终模块直接读取已检索原文”机制：保留四次模型请求和两跳检索，仅最终模块新增 evidence 输入，以通用姓名/实体表述指令替换标题强制规则；不增加新检索、反思或GEPA优化。验证集比较后冻结，再执行新测试集三组对照。此修改与GEPA候选搜索相互独立。', '',
        '## 数据隔离与预算', '',
        '原30题测试已用于本轮诊断，之后只报告开发诊断成绩。新测试从首轮原测试母池随机种子20261026抽30题，排除原90题及所有首轮轨迹ID，先冻结ID，再验证集筛选后执行，期间不查看新题答案来修改程序。原30题可复跑作诊断；全部新增费用上限¥15，含失败和冒烟；不启动GEPA。预计三组×(30验证+30原测试+30新测试)=270次任务执行、约1080次模型请求，按R1每题约0.005–0.009元，约¥1.5–3；预算预留控制沿用客户端。价格按首轮配置估算，实际账单可能不同。', '',
        '本轮验收：归因报告、可开关单项改进、原7项及新增回归测试、三组验证/诊断/新测试真实结果、逐题变化与不确定性、成本账本、变更及最终报告。进度在 [[docs/round2-changelog|本轮变更与进度]] 持续记录。']
    (ROOT / 'docs/round2-badcase-analysis.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(dict(groups=dict(counts), categories=dict(cats), snapshot=str(snap)))


if __name__ == '__main__':
    main()
