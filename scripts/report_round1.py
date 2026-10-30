"""将完成的首轮实验汇总为可核对的中文结果报告。

需本地原始账本（runs/round1/api_calls.jsonl）；开源包只带脱敏汇总。
"""

import json
from pathlib import Path
from model_client import load_usage_records

try:
    from ._ledger import require_files
except ImportError:  # 以脚本方式直接运行
    from _ledger import require_files

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "runs/round1"


def main():
    require_files(RUN / "summary.json", RUN / "api_calls.jsonl")
    result = json.loads((RUN / "summary.json").read_text(encoding="utf-8"))
    if result["status"] != "completed":
        raise RuntimeError("实验尚未完成，不能生成完成报告")
    config = json.loads((RUN / "config.json").read_text(encoding="utf-8"))
    calls = load_usage_records(RUN)
    lines = ["# HotpotQA-Mini 首轮结果", "",
             "本结果是项目内部 benchmark 分数。它用于衡量 GEPA 基线及后续 Harness 自进化改进，赛事评分由组委会的作品评审决定。", "",
             f"任务模型：`{config['task_model']}`；反思模型：`{config['reflection_model']}`。优化指标为 EM，固定种子为 {config['seed']}。使用两跳 BM25 检索、每跳 7 篇文档、四个 DSPy ChainOfThought 模块。", "",
             "## 得分", "", "| 数据集 | 程序 | 题数 | EM | F1 | 两跳支持文档全覆盖率 | 单题平均秒数 |", "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for key, split, name in [("baseline_val", "验证", "初始"), ("optimized_val", "验证", "GEPA"),
                             ("baseline_test", "独立测试", "初始"), ("optimized_test", "独立测试", "GEPA")]:
        row = result[key]
        lines.append(f"| {split} | {name} | {row['count']} | {row['em'] * 100:.2f}% | {row['f1'] * 100:.2f}% | {row['all_support_retrieved'] * 100:.2f}% | {row['seconds']:.2f} |")
    before, after = result["baseline_test"], result["optimized_test"]
    lines += ["", f"独立测试 EM 变化：{(after['em'] - before['em']) * 100:+.2f} 个百分点；F1 变化：{(after['f1'] - before['f1']) * 100:+.2f} 个百分点。",
              f"原先错误、优化后答对 {len(result['improvements'])} 题；原先答对、优化后错误 {len(result['regressions'])} 题。", "",
              "## 搜索与费用", "",
              f"优化预算设为 {config['max_metric_calls']} 次 metric calls，GEPA 实际记录 {result['metric_calls']} 次，保留 {result['num_candidates']} 个候选，验证集选择候选索引 {result['best_candidate_index']}。",
              f"累计 API 请求 {result['api_calls']} 次；输入 {result['prompt_tokens']:,} Token，输出 {result['completion_tokens']:,} Token。模型费用按原价估算合计 **¥{result['cost_cny']:.4f}**，本轮授权上限 ¥{config['budget_cny']:.0f}。实际账单可能享有免费额度和缓存折扣。", "",
              "| 阶段 | 模型费用估算 |", "| --- | ---: |"]
    for phase, cost in sorted(result["phase_costs_cny"].items()):
        lines.append(f"| {phase} | ¥{cost:.4f} |")
    errors = sum("error" in call for call in calls)
    lines += ["", f"账本中包含 {errors} 次失败请求。没有返回用量的失败请求按预留上界记账。", "",
              "## 证据与解释边界", "",
              "配置、源代码版本、数据划分和复现方法见 [[docs/benchmark-round1|实验记录]] 与 [[docs/reproduction|复现说明]]。", "",
              "原始汇总：[[runs/round1/summary.json|summary.json]]；候选及验证成绩：[[runs/round1/gepa_result.json|GEPA 结果]]；固定最佳 Prompt：[[runs/round1/best_candidate.json|最佳候选]]；逐题轨迹：[[runs/round1/rollouts.jsonl|轨迹记录]]；逐请求用量和响应：[[runs/round1/api_calls.jsonl|API 账本]]。", "",
              "测试集只在验证集选择候选并冻结后执行；未根据测试结果回选候选。若最佳候选仍为初始候选，测试输出直接复用。", ""]
    lines += [f"- {item}。" for item in result["limitations"]]
    lines += ["", "本轮尚未加入自研 Harness 演化策略。任何 GEPA 优化增益均归属于当前 GEPA 基线设置，后续自主贡献需要在相同数据、模型和预算下做对照。", ""]
    path = ROOT / "docs/round1-results.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
