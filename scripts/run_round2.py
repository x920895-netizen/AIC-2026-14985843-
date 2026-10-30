"""冻结单项证据路由改进，执行三组配对评测并保存断点。"""
import concurrent.futures
import json
import os
import random
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / 'runs/round2'
os.environ.setdefault('DSPY_CACHEDIR', str(ROOT / '.cache/dspy'))
os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP', 'True')
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

import dspy
from hotpot_adapter import HotpotAdapter
from model_client import ModelClient
from retrieval import Retriever
from run_round1 import save, summarize


FINAL_INSTRUCTIONS = '''Answer the question using the summaries and the original retrieved evidence.
Verify the requested relationship against the original passages; summaries may omit details or confuse entities.
Document titles are retrieval labels, not mandatory answer strings. For a person, prefer the complete personal name explicitly given in the relevant passage body, excluding honorifics, dates and biographical descriptions. Preserve the source spelling. For other entities or attributes, return only the requested name or value, without explanatory qualifiers.
Do not expand a name using unsupported guesses. Resolve conflicting summaries using explicit evidence. If evidence is incomplete, use the identified entities carefully and avoid conflating different roles.
Return a concise answer in the answer field. Use yes or no for yes/no questions.'''


class Round2Client(ModelClient):
    def __init__(self, config, run_dir):
        super().__init__(config, run_dir)
        # Round 2 的 ¥15 独立额度只覆盖本轮；首轮校准账本保持原样。
        self.spent = sum(r['cost_cny'] for r in self.records())

    def records(self):
        return [json.loads(l) for l in self.path.read_text(encoding='utf-8').splitlines()] if self.path.exists() else []


def prepare_fresh():
    path = RUN / 'fresh_test.json'
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    import pyarrow.parquet as pq
    splits = json.loads((ROOT / 'data/splits.json').read_text(encoding='utf-8'))
    excluded = {r['id'] for rows in splits.values() for r in rows}
    excluded.update(json.loads(l)['id'] for l in (ROOT / 'runs/round1/rollouts.jsonl').read_text(encoding='utf-8').splitlines())
    # 仅从首轮测试母池抽样，读取后不打印问题/答案，不据此调整实现。
    table = pq.read_table(ROOT / 'data/train-00000-of-00002.parquet')
    indices = list(range(36178))
    random.Random(20261026).shuffle(indices)
    rows = []
    for i in indices:
        row = table.slice(i, 1).to_pylist()[0]
        if row['id'] in excluded:
            continue
        row.update(source_index=i, split='fresh_test')
        rows.append(row)
        if len(rows) == 30:
            break
    assert len(rows) == len({r['id'] for r in rows}) == 30
    save(path, rows)
    save(RUN / 'fresh_manifest.json', dict(seed=20261026, excluded_count=len(excluded),
        ids=[r['id'] for r in rows], source_indices=[r['source_index'] for r in rows],
        independent=True, note='验证前冻结；未将新测试结果用于修改或选择候选'))
    return rows


def phase(adapter, rows, candidate, name):
    adapter.client.phase = name
    old = {r['id']: r for r in adapter.completed if r['phase'] == name}
    pending = [r for r in rows if r['id'] not in old]
    # 直接执行避免首轮适配器跨候选验证缓存复用，所有组均为本轮真实请求。
    with concurrent.futures.ThreadPoolExecutor(max_workers=adapter.workers) as pool:
        for r in pool.map(lambda row: adapter.execute(row, candidate), pending):
            old[r['id']] = r
    result = [old[r['id']] for r in rows]
    save(RUN / (name + '.json'), result)
    print(name, summarize(result), flush=True)
    return result


def main():
    RUN.mkdir(exist_ok=True, parents=True)
    config = json.loads((ROOT / 'runs/round1/config.json').read_text(encoding='utf-8'))
    config.update(budget_cny=15.0)
    save(RUN / 'config.json', config)
    seed = json.loads((ROOT / 'runs/round1/seed_candidate.json').read_text(encoding='utf-8'))
    best = json.loads((ROOT / 'runs/round1/best_candidate.json').read_text(encoding='utf-8'))
    r2 = dict(best)
    r2['final_answer.predict'] = FINAL_INSTRUCTIONS
    if (RUN / 'r2_candidate.json').exists():
        assert json.loads((RUN / 'r2_candidate.json').read_text(encoding='utf-8')) == r2
    save(RUN / 'r2_candidate.json', r2)
    fresh = prepare_fresh()
    splits = json.loads((ROOT / 'data/splits.json').read_text(encoding='utf-8'))
    client = Round2Client(config, RUN)
    from model_client import TaskLM
    lm = TaskLM(client)
    dspy.configure(lm=lm, adapter=dspy.ChatAdapter(), disable_history=True)
    retriever = Retriever(ROOT / 'data')
    normal = HotpotAdapter(ROOT, retriever, lm, client, RUN, workers=config['workers'])
    direct = HotpotAdapter(ROOT, retriever, lm, client, RUN, workers=config['workers'], direct_evidence=True)
    smoke = phase(direct, splits['train'][:2], r2, 'smoke_r2')
    assert all(len(r['steps']) == 4 and 'evidence' in r['steps'][-1]['inputs'] for r in smoke)
    groups = [('Baseline', normal, seed), ('GEPA-R1', normal, best), ('GEPA-R2', direct, r2)]
    results = {}
    for split, rows in [('val', splits['val']), ('diagnostic_test', splits['test']), ('fresh_test', fresh)]:
        if split == 'diagnostic_test':
            a = results['val_GEPA-R1']; b = results['val_GEPA-R2']
            # 单一候选，无超参搜索。验证集EM不低于R1才将其视为推荐配置。
            save(RUN / 'frozen_selection.json', dict(recommended=sum(r['em'] for r in b) >= sum(r['em'] for r in a),
                criterion='验证集EM不低于本轮R1；仍完整报告R2失败和独立测试结果', frozen_at_utc=datetime.now(timezone.utc).isoformat(),
                candidate=r2, search_candidates=1))
        for name, adapter, candidate in groups:
            key = split + '_' + name
            results[key] = phase(adapter, rows, candidate, key)
    calls = client.records()
    save(RUN / 'summary.json', dict(status='completed', completed_at_utc=datetime.now(timezone.utc).isoformat(),
        metrics={k: summarize(v) for k,v in results.items()}, cost_cny=sum(r['cost_cny'] for r in calls),
        api_calls=len(calls), prompt_tokens=sum(r.get('usage',{}).get('prompt_tokens',0) for r in calls),
        completion_tokens=sum(r.get('usage',{}).get('completion_tokens',0) for r in calls),
        failures=sum(bool(r.get('error')) for r in calls), evaluations=sum(len(v) for v in results.values())+len(smoke),
        wall_seconds_this_process=time.monotonic()-START))


if __name__ == '__main__':
    START = time.monotonic()
    awake = False
    if os.name == 'nt':
        import ctypes
        awake = bool(ctypes.windll.kernel32.SetThreadExecutionState(0x80000001))
    try:
        main()
    except Exception as exc:
        save(RUN / 'failure.json', dict(error=str(exc), traceback=traceback.format_exc()))
        raise
    finally:
        if awake:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
