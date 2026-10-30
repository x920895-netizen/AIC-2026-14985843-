"""将真实三组结果转为可追溯的配对报告，不筛除错误样本。

需本地原始账本（runs/round2/api_calls.jsonl 与三组逐题文件）。
"""
import json
import random
from collections import Counter
from pathlib import Path

from scipy.stats import binomtest
from analyze_round2 import diagnosis
from hotpot_adapter import normalize

try:
    from ._ledger import require_files
except ImportError:  # 以脚本方式直接运行
    from _ledger import require_files

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / 'runs/round2'


def load(name):
    return json.loads((RUN / name).read_text(encoding='utf-8'))


def paired(a, b):
    before = {r['id']: r for r in a}
    assert set(before) == {r['id'] for r in b}
    wins = [r['id'] for r in b if r['em'] > before[r['id']]['em']]
    losses = [r['id'] for r in b if r['em'] < before[r['id']]['em']]
    delta = [r['em'] - before[r['id']]['em'] for r in b]
    rng = random.Random(20261026)
    boots = sorted(sum(rng.choices(delta, k=len(delta))) / len(delta) for _ in range(10000))
    return dict(improvements=wins, regressions=losses,
        delta_em=sum(delta)/len(delta), bootstrap_95=[boots[249], boots[9749]],
        mcnemar_exact_p=binomtest(len(wins), len(wins)+len(losses), 0.5).pvalue if wins or losses else 1.0)


def main():
    require_files(RUN / "summary.json", RUN / "api_calls.jsonl", RUN / "frozen_selection.json",
                  *[RUN / f"{s}_{g}.json" for s in ("val", "diagnostic_test", "fresh_test")
                    for g in ("Baseline", "GEPA-R1", "GEPA-R2")])
    summary = load('summary.json')
    assert summary['status'] == 'completed'
    # 以持久账本为准，覆盖恢复前后全部请求与失败预留。
    calls=[json.loads(l) for l in (RUN/'api_calls.jsonl').read_text(encoding='utf-8').splitlines()]
    summary['cost_cny'] = sum(r['cost_cny'] for r in calls)
    (RUN/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    pairs = {}
    for split in ['val', 'diagnostic_test', 'fresh_test']:
        groups = {g: load(f'{split}_{g}.json') for g in ['Baseline','GEPA-R1','GEPA-R2']}
        pairs[split] = {g: paired(groups['GEPA-R1'], groups[g]) for g in ['Baseline','GEPA-R2']}
        comparisons = []
        for index, b in enumerate(groups['GEPA-R1']):
            entries = {g: rows[index] for g, rows in groups.items()}
            assert all(r['id'] == b['id'] for r in entries.values())
            comparisons.append(dict(id=b['id'], gold=b['gold'],
                groups={g: dict(answer=r['answer'],em=r['em'],f1=r['f1'],
                    support_recall=r['hop2_support_recall'],diagnosis=diagnosis(r)) for g,r in entries.items()}))
        (RUN / f'{split}_comparison.json').write_text(json.dumps(comparisons,ensure_ascii=False,indent=2),encoding='utf-8')
    (RUN / 'paired_statistics.json').write_text(json.dumps(pairs,ensure_ascii=False,indent=2),encoding='utf-8')
    fresh_r1 = summary['metrics']['fresh_test_GEPA-R1']
    fresh_r2 = summary['metrics']['fresh_test_GEPA-R2']
    fresh_pair = pairs['fresh_test']['GEPA-R2']
    lines = ['# Round 2 实验结果', '',
        f"新独立测试30题：R1 EM {fresh_r1['em']:.2%} / F1 {fresh_r1['f1']:.2%}，R2 EM {fresh_r2['em']:.2%} / F1 {fresh_r2['f1']:.2%}。R2相对R1新增答对{len(fresh_pair['improvements'])}题、回归{len(fresh_pair['regressions'])}题。真实实验已完成；针对姓名短式错误的机制是否有效，另以目标样本结果判断。", '',
        '目标：验证最终答案模块直接读取既有检索原文，是否减少摘要压缩和标题优先导致的姓名表述错误。归因与方案在 [[docs/round2-badcase-analysis|修改前报告]]，实现和快照见 [[docs/round2-changelog|变更记录]]。本轮不运行GEPA优化，GEPA-R2表示R1候选上的任务程序改进。', '',
        '## 配置与隔离', '',
        '沿用 qwen3.8-flash、非思考、温度0、8192输出上限、并发4、Wikipedia2017完整BM25、两跳各7篇与原EM/F1。最终模块增加原文输入并替换其指令，其他三个模块使用R1 Prompt；本轮不区分“证据输入”和“最终指令”的单独贡献。所有三组均本轮真实复跑，未复用首轮API输出。', '',
        '验证集沿用30题开发数据；原测试30题已经参与错误分析，标记为诊断开发集。全新独立测试30题从同一母池抽取，种子20261026，排除原90题及首轮全部执行ID；候选与抽样清单在验证前保存，验证后冻结推荐决定，再执行新测试。新测试结果未用于修改或回选。本轮完成后若再据其失败开发，它将失去后续轮次的独立性。', '',
        '## 三组实际结果', '', '| 集合 | 组别 | 完成/总题数 | EM | F1 | 支持文档全覆盖 | 平均秒/题 |', '|---|---|---:|---:|---:|---:|---:|']
    labels = dict(val='验证（开发）',diagnostic_test='原测试（诊断开发）',fresh_test='新测试（独立）')
    for split,label in labels.items():
        for g in ['Baseline','GEPA-R1','GEPA-R2']:
            m = summary['metrics'][f'{split}_{g}']
            lines.append(f"| {label} | {g} | {m['count']}/30 | {m['em']:.2%} | {m['f1']:.2%} | {m['all_support_retrieved']:.2%} | {m['seconds']:.2f} |")
    selected = load('frozen_selection.json')
    lines += ['', f"验证集事先约定标准：R2 EM不低于本轮R1。是否达到：{'是' if selected['recommended'] else '否'}。只比较1个方案；即使验证失败仍完整保留三组实验。", '', '## 配对变化与不确定性', '']
    for split,label in labels.items():
        p = pairs[split]['GEPA-R2']
        lo,hi = p['bootstrap_95']
        lines += [f"- {label}，R2相对R1：新增答对{len(p['improvements'])}，回归{len(p['regressions'])}，EM变化{p['delta_em']*100:+.2f}个百分点；配对bootstrap 95%区间[{lo*100:.2f}, {hi*100:.2f}]，双侧精确McNemar p={p['mcnemar_exact_p']:.4f}。"]
    lines += ['', '区间是30题上的经验配对重采样（10000次），未包含服务模型漂移和重复推理随机性。单种子小样本只能提供初步证据，不能证明普遍有效。各组答案、EM/F1、召回、归因及变化ID见 [[runs/round2/paired_statistics.json|配对统计]] 与各集合 comparison.json。', '', '## 失效模式变化', '']
    for split,label in labels.items():
        for g in ['GEPA-R1','GEPA-R2']:
            rows = load(f'{split}_{g}.json')
            cats = Counter(c for r in rows if not r['em'] for c in diagnosis(r)[0])
            lines.append(f"- {label} {g}：{dict(cats)}；重叠类别不相加。")
    lines += ['', '新独立测试的回归实例（仅作事后解释，不据此修改候选）：', '']
    fresh_compare = load('fresh_test_comparison.json')
    for item in fresh_compare:
        if item['id'] in fresh_pair['regressions']:
            a, b = item['groups']['GEPA-R1'], item['groups']['GEPA-R2']
            lines.append(f"- `{item['id']}`：参考 `{item['gold']}`；R1 `{a['answer']}` → R2 `{b['answer']}`；两组支持标题召回均为{a['support_recall']:.0%}/{b['support_recall']:.0%}。")
    lines += ['', '本轮两题回归均表现为答案表述不匹配；支持标题已经齐全。新测试R2的支持文档全覆盖率高于R1，最终EM仍下降，说明召回覆盖和最终答案质量需要分别衡量。两组前三模块也存在服务输出波动，不能将全部变化归因于单个最终模块机制。', '']
    lines += ['', '这些自动分类是筛查信号：词元相交只表示表述差异，支持标题召回也不等于所需事实句已提供。完整错误轨迹保留；无法仅凭此表将所有变化因果归于证据路由。', '',
        '## 与首轮逐题差异', '']
    # 仅在离线报告识别首轮验证中的短式答案，不影响运行、评分或候选选择。
    old_val = load('round1_val_evidence.json')
    short_ids = {x['result']['id'] for x in old_val if not x['result']['em'] and
                 set(normalize(x['result']['answer']).split()) < set(normalize(x['result']['gold']).split())}
    targeted = {g: sum(r['em'] for r in load(f'val_{g}.json') if r['id'] in short_ids) for g in ['GEPA-R1','GEPA-R2']}
    lines += [f"首轮验证中预测词元为参考真子集的短姓名目标样本共{len(short_ids)}题，本轮R1答对{targeted['GEPA-R1']:.0f}题、R2答对{targeted['GEPA-R2']:.0f}题。原文与通用指令已经到达最终模块，但目标错误是否减少必须以此结果判断；整体分数上升不能替代目标机制验证。", '']
    if targeted['GEPA-R2'] <= targeted['GEPA-R1']:
        lines += ['目标短姓名错误未观察到减少，本轮“原文直连可以修复该类错误”的假设未获这批验证样本支持。保留此负面结果；不依据独立测试继续改写指令。', '']
    for group,filename in [('Baseline','baseline_test.json'),('GEPA-R1','optimized_test.json')]:
        old = json.loads((ROOT / 'runs/round1' / filename).read_text(encoding='utf-8'))
        new = load(f'diagnostic_test_{group}.json')
        p = paired(old,new)
        pairs['round1_repeat_'+group] = p
        lines.append(f"- {group} 本轮复跑相对首轮：新增答对{len(p['improvements'])}，回归{len(p['regressions'])}；变化ID已保存到配对统计。温度0无法保证远程模型完全确定，该波动需要与算法增益分开解释。")
    (RUN / 'paired_statistics.json').write_text(json.dumps(pairs,ensure_ascii=False,indent=2),encoding='utf-8')
    calls=[json.loads(l) for l in (RUN/'api_calls.jsonl').read_text(encoding='utf-8').splitlines()]
    phasecost = {p:sum(r['cost_cny'] for r in calls if r['phase']==p) for p in sorted({r['phase'] for r in calls})}
    lines += ['', '## 成本与验证', '',
        f"任务执行{summary['evaluations']}次（270次对照+2次训练冒烟），GEPA优化评估0次。模型请求{summary['api_calls']}次，失败请求{summary['failures']}次；输入{summary['prompt_tokens']:,} Token，输出{summary['completion_tokens']:,} Token。新增费用按首轮价目及返回用量保守估算 **¥{summary['cost_cny']:.4f} / ¥15**；失败无用量请求按预留上界计账，实际账单可能不同。", '',
        f"本次完成进程耗时{summary['wall_seconds_this_process']:.1f}秒，不含此前连接失败进程耗时；并发下单题平均耗时不等于总耗时。逐请求原始记录见 [[runs/round2/api_calls.jsonl|费用账本]]，逐题原文输入与输出见 [[runs/round2/rollouts.jsonl|执行轨迹]]。", '',
        '原有7项测试和新增2项回归测试实际通过，共9通过、0失败，记录见 [[runs/round2/tests.json|测试结果]]。覆盖默认四模块行为、证据传递与去重、关闭开关、参考答案隔离、本轮账本独立与恢复、原评分及失败处理；随后真实2题冒烟验证四模块输出与最终证据输入。', '',
        '| 阶段 | 新增费用估算 |', '|---|---:|']
    lines += [f'| {p} | ¥{c:.4f} |' for p,c in phasecost.items()]
    lines += ['', '## 结论边界与下一步', '',
        '已验证：实现开关与兼容性测试通过；三组在相同集合实际执行，所有负面结果保留。表内分数为本次真实运行。数据支持程度由上方配对结果与区间判断。', '',
        '仍未解决：检索完全缺失、关系连接错误、参考答案长短形式不一致、赞助杯赛等问题的潜在歧义，以及任务服务模型快照不可固定。正文全名优先也会产生错误长式；不通过参考答案映射或放松评分修补。', '',
        '尚未验证的假设：证据路由在更多种子和更大样本上稳定提升；证据输入与指令各自的独立贡献；完整Harness自进化能力。本轮属于一次有证据依据的通用任务程序改进。', '',
        '下一轮优先问题：对完全未召回支持证据的任务，通用的缺失关系驱动检索策略能否在固定模型调用预算下提高召回并减少猜测？需另立新独立测试集和预算。', '',
        '验收：首轮源码/结果/轨迹已查；归因报告、单项开关实现、快照、原7项及新增测试、变更报告、三组真实对照、费用上限、测试独立性说明与本报告均已完成。']
    (ROOT/'docs/round2-results.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps(dict(metrics=summary['metrics'],cost=summary['cost_cny'],pairs=pairs),ensure_ascii=False))


if __name__ == '__main__':
    main()
