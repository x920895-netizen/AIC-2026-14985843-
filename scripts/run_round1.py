"""运行模型连通检查、冒烟、GEPA 优化与独立测试，保存完整实验产物。"""

import argparse
import importlib.metadata
import json
import os
import platform
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Windows 下由启动命令的 -X utf8 统一文件编码，标准流也显式设为 UTF-8。
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
if os.name == "nt" and not sys.flags.utf8_mode:
    raise RuntimeError("Windows 运行本脚本需要 python -X utf8 scripts/run_round1.py，以保证 GEPA 日志可写入外文字符")

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DSPY_CACHEDIR", str(ROOT / ".cache/dspy"))
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("PYTHONHASHSEED", "1")

import dspy
import gepa
from hotpot_adapter import HotpotAdapter
from model_client import ModelClient, TaskLM, load_usage_records
from retrieval import Retriever

RUN = ROOT / "runs/round1"


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(temporary, path)


def summarize(rows):
    keys = ["em", "f1", "hop1_support_recall", "hop2_support_recall", "all_support_retrieved", "seconds"]
    return {"count": len(rows), **{key: sum(row[key] for row in rows) / len(rows) for key in keys}}


def evaluate_phase(adapter, rows, candidate, phase):
    path = RUN / f"{phase}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    adapter.client.phase = phase
    previous = {r["id"]: r for r in adapter.completed if r["phase"] == phase and r["candidate"] == candidate}
    pending = [r for r in rows if r["id"] not in previous]
    if pending:
        previous.update({r["id"]: r for r in adapter.evaluate(pending, candidate).outputs})
    results = [previous[r["id"]] for r in rows]
    save(path, results)
    print(f"{phase}：{summarize(results)}", flush=True)
    return results


def probe(client, lm):
    if (RUN / "probe.json").exists():
        return
    client.phase = "probe"
    with dspy.context(lm=lm):
        pred = dspy.ChainOfThought("question -> answer")(question="What is 2 + 2? Return the number.")
    if str(pred.answer).strip() != "4":
        raise RuntimeError(f"任务模型结构化连通检查结果异常：{pred}")
    reflection = client.reflect("This is a connection check. Reply with OK only.")
    if reflection.strip().upper() != "OK":
        raise RuntimeError(f"反思模型连通检查结果异常：{reflection}")
    save(RUN / "probe.json", {"task": pred.toDict(), "reflection": reflection, "cost_cny": client.spent})
    print(f"两个模型推理与 DSPy 输出解析通过，费用估算 {client.spent:.4f} 元", flush=True)


def main(probe_only=False, wait_data=False):
    RUN.mkdir(parents=True, exist_ok=True)
    config = json.loads((ROOT / "benchmark_config.json").read_text(encoding="utf-8"))
    if (RUN / "config.json").exists():
        if json.loads((RUN / "config.json").read_text(encoding="utf-8")) != config:
            raise RuntimeError("运行目录配置与当前配置不同，请显式创建新的实验轮次")
    save(RUN / "config.json", config)
    versions = {name: importlib.metadata.version(name) for name in ["gepa", "dspy", "bm25s", "PyStemmer", "openai", "numpy", "scipy", "pyarrow"]}
    save(RUN / "environment.json", {"python": sys.version, "platform": platform.platform(), "packages": versions,
         "gepa_commit": "7af5f0c5c79fcc16b2d5248b187d0864683edd4d", "artifact_commit": "cbefbc1aa0f43dd39874ec4bf42211365dbda42e"})
    client = ModelClient(config, RUN)
    lm = TaskLM(client)
    dspy.configure(lm=lm, adapter=dspy.ChatAdapter(), disable_history=True)
    probe(client, lm)
    if probe_only:
        return
    if wait_data:
        wait_start = time.monotonic()
        while not all((ROOT / "data" / name).exists() for name in ["splits.json", "wiki.abstracts.2017.tar.gz"]):
            if time.monotonic() - wait_start > 3600:
                raise TimeoutError("等待数据准备超过一小时，请检查下载进程")
            time.sleep(5)
    started = time.monotonic()
    splits = json.loads((ROOT / "data/splits.json").read_text(encoding="utf-8"))
    retriever = Retriever(ROOT / "data")
    adapter = HotpotAdapter(ROOT, retriever, lm, client, RUN, workers=config["workers"])
    seed = adapter.seed_candidate()
    save(RUN / "seed_candidate.json", seed)
    smoke = evaluate_phase(adapter, splits["train"][:8], seed, "smoke")
    smoke_calls = [json.loads(line) for line in client.path.read_text(encoding="utf-8").splitlines()]
    smoke_calls = [r for r in smoke_calls if r["phase"] == "smoke"]
    smoke_cost = sum(r["cost_cny"] for r in smoke_calls)
    smoke_stats = {"metrics": summarize(smoke), "cost_cny": smoke_cost,
        "task_cost_per_rollout_cny": smoke_cost / len(smoke),
        "estimated_task_cost_for_450_plus_60_test_cny": smoke_cost / len(smoke) * 510}
    save(RUN / "smoke_summary.json", smoke_stats)
    print(f"冒烟检查完成：{smoke_stats}", flush=True)
    if not (RUN / "gepa_result.json").exists():
        client.phase = "optimization"
        print("开始 GEPA：EM，30 题训练、30 题验证，max_metric_calls=450", flush=True)
        result = gepa.optimize(seed_candidate=seed, trainset=splits["train"], valset=splits["val"],
            adapter=adapter, reflection_lm=client.reflect, max_metric_calls=config["max_metric_calls"],
            seed=config["seed"], reflection_minibatch_size=3, use_merge=False,
            run_dir=str(RUN / "gepa"), cache_evaluation=True, write_agent_state=True,
            raise_on_exception=True)
        save(RUN / "gepa_result.json", result.to_dict())
    result_dict = json.loads((RUN / "gepa_result.json").read_text(encoding="utf-8"))
    best_idx = max(range(len(result_dict["candidates"])), key=lambda i: result_dict["val_aggregate_scores"][i])
    best = result_dict["candidates"][best_idx]
    # 先冻结验证集选择结果，再首次执行独立测试题的两组模型调用。
    save(RUN / "best_candidate.json", best)
    baseline_test = evaluate_phase(adapter, splits["test"], seed, "baseline_test")
    if best == seed:
        optimized_test = baseline_test
        save(RUN / "optimized_test.json", baseline_test)
    else:
        optimized_test = evaluate_phase(adapter, splits["test"], best, "optimized_test")
    baseline_val = {r["id"]: r for r in adapter.completed if r["split"] == "val" and r["candidate"] == seed}
    best_val = {r["id"]: r for r in adapter.completed if r["split"] == "val" and r["candidate"] == best}
    assert len(baseline_val) == len(best_val) == 30
    calls = load_usage_records(RUN)
    correct_before = sum(r["em"] == 1 for r in baseline_test)
    regressed = [b["id"] for b, o in zip(baseline_test, optimized_test) if b["em"] == 1 and o["em"] == 0]
    improved = [b["id"] for b, o in zip(baseline_test, optimized_test) if b["em"] == 0 and o["em"] == 1]
    summary = {"status": "completed", "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "smoke": summarize(smoke), "baseline_val": summarize(list(baseline_val.values())),
        "optimized_val": summarize(list(best_val.values())), "baseline_test": summarize(baseline_test),
        "optimized_test": summarize(optimized_test), "best_candidate_index": best_idx,
        "num_candidates": len(result_dict["candidates"]), "metric_calls": result_dict["total_metric_calls"],
        "cost_cny": client.spent, "api_calls": len(calls),
        "prompt_tokens": sum(r.get("usage", {}).get("prompt_tokens", 0) for r in calls),
        "completion_tokens": sum(r.get("usage", {}).get("completion_tokens", 0) for r in calls),
        "phase_costs_cny": {p: sum(r["cost_cny"] for r in calls if r["phase"] == p) for p in {r["phase"] for r in calls}},
        "regressions": regressed, "improvements": improved,
        "regression_rate_among_baseline_correct": len(regressed) / correct_before if correct_before else None,
        "current_process_seconds": time.monotonic() - started,
        "limitations": ["单次随机划分，每组独立测试仅30题", "任务模型与论文Qwen3-8B不同", "模型费用根据官方原价和API用量估算，实际账单可能受缓存与免费额度影响", "任务模型采用非思考模式，四个DSPy模块仍生成reasoning字段", "当前任务模型ID未提供固定快照映射"]}
    save(RUN / "summary.json", summary)
    print("首轮完成：" + json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--wait-data", action="store_true")
    args = parser.parse_args()
    awake_request = False
    if os.name == "nt":
        import ctypes
        awake_request = bool(ctypes.windll.kernel32.SetThreadExecutionState(0x80000001))
        print("已申请本轮进程运行期间保持系统唤醒" if awake_request else "警告：未能申请保持唤醒，继续执行评测", flush=True)
    try:
        main(probe_only=args.probe_only, wait_data=args.wait_data)
    except Exception as exc:
        RUN.mkdir(parents=True, exist_ok=True)
        save(RUN / "failure.json", {"error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc(),
                                   "time_utc": datetime.now(timezone.utc).isoformat()})
        raise
    finally:
        if awake_request:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
