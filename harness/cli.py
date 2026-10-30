"""命令行入口：初始化、单次运行、在线闭环、报告、凭证复核与回滚。

示例::

    python -m harness.cli init --db demo-state
    python -m harness.cli run --db demo-state --dataset data.json --runner mock --output run.json
    python -m harness.cli evolve --db demo-state --dataset data.json --proposer mock --runner mock
    python -m harness.cli report --db demo-state --out report.md
    python -m harness.cli stats --db demo-state
    python -m harness.cli trace --db demo-state --raw
    python -m harness.cli protocol-export --db demo-state --out protocol.json
    python -m harness.cli verify-receipts --db demo-state
"""

import argparse
import json
import sys
from pathlib import Path

from .contracts import GateSpec, RuntimeTask
from .controller import EvolutionFoundation
from .execution import OnlineExecutor
from .protocol import ProtocolSpec, build_protocol, protocol_warnings
from .receipt import verify_chain
from .report import write as write_report
from .runners.mock import MockRunner
from .runners.openai_compatible import OpenAICompatibleRunner
from .proposers.mock import MockProposer
from .proposers.reflective import ReflectiveProposer
from .storage import FoundationStore, IntegrityError, StateConflict
from .trace import TraceLog

EMPTY_ARTIFACTS = {
    "prompt": {"components": {"final_answer.predict": "请回答问题，只输出最终答案。"}},
    "memory": {"experience_ids": [], "retrieval_policy": {}, "lifecycle_policy": {}},
    "tool": {"tools": [], "selection_policy": {}, "risk_constraints": {}},
    "skill": {"definitions": [], "selection_policy": {}, "lifecycle_policy": {}},
}


def _add_state(p):
    p.add_argument("--db", "--state-dir", dest="state_dir", required=True,
                   help="状态目录（含 foundation.sqlite3）")


def _add_runner(p):
    p.add_argument("--runner", choices=["mock", "openai"], default="mock")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--base-url", default="https://api.openai.com/v1")
    p.add_argument("--model", default="gpt-4o-mini")
    p.add_argument("--api-key-env", default="OPENAI_API_KEY")
    p.add_argument("--max-retries", type=int, default=4)
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--input-price", type=float, default=0.0, help="元/百万输入 token")
    p.add_argument("--output-price", type=float, default=0.0, help="元/百万输出 token")
    p.add_argument("--json-mode", action="store_true", help="请求 response_format=json_object")
    p.add_argument("--record-raw", action="store_true", help="在运行记录里保存原始响应")
    p.add_argument("--rate-limit", type=float, default=0.0,
                   help="每秒请求数上限（令牌桶）；0 表示不限流")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evidence-evolve", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="初始化一个新的状态目录与基线版本")
    _add_state(init)
    init.add_argument("--model", default="offline-deterministic")
    init.set_defaults(func=cmd_init)

    run = sub.add_parser("run", help="在冻结清单上跑一次并落库")
    _add_state(run)
    run.add_argument("--dataset", required=True, help="数据集 JSON（tasks/labels）")
    run.add_argument("--version", default=None, help="默认使用当前版本")
    run.add_argument("--output", default=None, help="运行摘要 JSON 输出路径")
    run.add_argument("--limit", type=float, default=1.0, help="本次预算上限（元）")
    _add_runner(run)
    run.set_defaults(func=cmd_run)

    evolve = sub.add_parser("evolve", help="跑在线自进化闭环（验证集选择，测试集只汇报）")
    _add_state(evolve)
    evolve.add_argument("--dataset", required=True, help="数据集 JSON，可含 val_task_ids/test_task_ids")
    evolve.add_argument("--proposer", choices=["mock", "reflective"], default="mock")
    evolve.add_argument("--strategy", choices=["good_patch", "bad_patch"], default="good_patch")
    evolve.add_argument("--gate", choices=["screen", "release"], default="release")
    evolve.add_argument("--rounds", type=int, default=3)
    evolve.add_argument("--budget", type=float, default=5.0)
    evolve.add_argument("--output", default=None, help="演化摘要 JSON 输出路径")
    evolve.add_argument("--report", default=None, help="自动生成的 Markdown 报告路径")
    evolve.add_argument("--protocol", default=None, help="导入协议 JSON；默认按数据集自动生成")
    evolve.add_argument("--protocol-out", default=None, help="协议快照输出路径（默认写入状态目录）")
    evolve.add_argument("--protocol-id", default=None, help="协议标识（默认 <数据集>-<档位>-v1）")
    evolve.add_argument("--repeats", type=int, default=1, help="每题重复次数")
    evolve.add_argument("--seed", type=int, default=1, help="bootstrap 随机种子")
    evolve.add_argument("--confidence", type=float, default=0.95)
    evolve.add_argument("--min-sample", type=int, default=30,
                        help="低于该题数时统计判据输出“证据不足”")
    _add_runner(evolve)
    evolve.set_defaults(func=cmd_evolve)

    stats = sub.add_parser("stats", help="重算一次演化的统计视图（Wilson/McNemar/BCa/MDE）")
    _add_state(stats)
    stats.add_argument("--run", default=None, help="演化 run id；默认取最近一次")
    stats.set_defaults(func=cmd_stats)

    trace = sub.add_parser("trace", help="读取 JSONL 事件日志（默认输出摘要）")
    _add_state(trace)
    trace.add_argument("--raw", action="store_true", help="逐行输出原始事件")
    trace.add_argument("--event", default=None, help="只输出指定事件")
    trace.add_argument("--limit", type=int, default=None, help="只输出最后 N 条")
    trace.set_defaults(func=cmd_trace)

    proto_export = sub.add_parser("protocol-export", help="导出协议（默认取最近一次演化）")
    _add_state(proto_export)
    proto_export.add_argument("--out", required=True)
    proto_export.add_argument("--run", default=None)
    proto_export.add_argument("--dataset", default=None, help="没有演化记录时从数据集生成")
    proto_export.set_defaults(func=cmd_protocol_export)

    proto_import = sub.add_parser("protocol-import", help="校验并导入协议 JSON")
    _add_state(proto_import)
    proto_import.add_argument("--file", required=True)
    proto_import.set_defaults(func=cmd_protocol_import)

    proto_show = sub.add_parser("protocol-show", help="显示最近一次演化的协议与警告")
    _add_state(proto_show)
    proto_show.add_argument("--run", default=None)
    proto_show.set_defaults(func=cmd_protocol_show)

    report = sub.add_parser("report", help="从状态库生成 Markdown 报告")
    _add_state(report)
    report.add_argument("--out", required=True)
    report.add_argument("--run", default=None, help="evolution run id；默认取最近一次")
    report.set_defaults(func=cmd_report)

    inspect = sub.add_parser("inspect", help="输出当前版本、候选、决策与凭证")
    _add_state(inspect)
    inspect.set_defaults(func=cmd_inspect)

    receipts = sub.add_parser("receipts", help="按链式顺序输出决策凭证")
    _add_state(receipts)
    receipts.set_defaults(func=cmd_receipts)

    verify = sub.add_parser("verify-receipts", help="复核凭证链完整性（篡改/断链返回非零）")
    _add_state(verify)
    verify.set_defaults(func=cmd_verify)

    rollback = sub.add_parser("rollback", help="显式回滚到祖先版本")
    _add_state(rollback)
    rollback.add_argument("--version", required=True)
    rollback.add_argument("--reason", required=True)
    rollback.add_argument("--allow-simulated", action="store_true")
    rollback.set_defaults(func=cmd_rollback)
    return parser


# ---- 通用 ----

def _rate_limit(args):
    value = float(getattr(args, "rate_limit", 0.0) or 0.0)
    return value if value > 0 else None


def _store(args) -> FoundationStore:
    root = Path(args.state_dir)
    if not (root/"foundation.sqlite3").is_file():
        raise FileNotFoundError(f"状态目录不存在或缺少 foundation.sqlite3：{root}（先运行 init）")
    return FoundationStore(root)


def _load_dataset(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    tasks = data.get("tasks")
    labels = data.get("labels")
    if not isinstance(tasks, list) or not isinstance(labels, dict) or not tasks:
        raise ValueError("数据集 JSON 需要 tasks 数组与 labels 对象")
    runtime = [RuntimeTask(item["task_id"], item["question"]) for item in tasks]
    ids = [t.task_id for t in runtime]
    if len(set(ids)) != len(ids) or set(ids) != set(labels):
        raise ValueError("tasks 与 labels 必须一一对应且唯一")
    return dict(dataset_id=data.get("dataset_id") or Path(path).stem, tasks=runtime, labels=labels,
                val_task_ids=data.get("val_task_ids"), test_task_ids=data.get("test_task_ids"))


def _ensure_dataset(store, engine, dataset) -> None:
    try:
        store.read("datasets", dataset["dataset_id"])
    except KeyError:
        engine.register_dataset(dataset["dataset_id"], dataset["tasks"], dataset["labels"])


def _build_runner(args):
    if args.runner == "mock":
        return MockRunner(lambda task, prompt, artifacts: f"mock-answer:{task.task_id}", workers=args.workers)
    return OpenAICompatibleRunner(base_url=args.base_url, model=args.model, api_key_env=args.api_key_env,
                                  workers=args.workers, max_retries=args.max_retries, timeout=args.timeout,
                                  input_price_cny_per_million=args.input_price,
                                  output_price_cny_per_million=args.output_price,
                                  response_format={"type": "json_object"} if args.json_mode else None,
                                  record_raw=args.record_raw)


def _split_ids(dataset):
    ids = [t.task_id for t in dataset["tasks"]]
    val = dataset.get("val_task_ids") or ids[:(len(ids)+1)//2]
    test = dataset.get("test_task_ids") or ids[(len(ids)+1)//2:]
    return list(val), list(test)


def _write_json(path, value):
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


# ---- 命令 ----

def cmd_init(args) -> int:
    root = Path(args.state_dir)
    if (root/"foundation.sqlite3").is_file():
        raise StateConflict("状态目录已存在状态库，init 不会覆盖；请换一个目录")
    store = FoundationStore(root)
    refs = {kind: store.write_artifact(kind, value) for kind, value in EMPTY_ARTIFACTS.items()}
    version = store.initialize(refs, {"model_config": {"model": args.model, "temperature": 0}})
    print(json.dumps({"state_dir": str(root), "version_id": version["version_id"]}, ensure_ascii=False))
    return 0


def cmd_run(args) -> int:
    store = _store(args)
    engine = EvolutionFoundation(store)
    dataset = _load_dataset(args.dataset)
    _ensure_dataset(store, engine, dataset)
    version_id = args.version or store.current()["version_id"]
    version = store.version(version_id)
    spec = GateSpec.release(dataset["dataset_id"], tuple(t.task_id for t in dataset["tasks"]),
                            version["config_snapshot"]["model_config"],
                            max_total_cost_cny=args.limit, require_offline=False)
    scope = engine.budget.create(args.limit)
    runner = _build_runner(args)
    run = engine.run(version_id, spec, scope["scope_id"], runner,
                     executor=OnlineExecutor(store, workers=args.workers,
                                             rate_limit=_rate_limit(args)))
    payload = dict(run_id=run["run_id"], version_id=version_id, status=run["status"],
                   mode=run["mode"], tasks=len(run["outputs"]), api_calls=run["api_calls"],
                   cost_cny=run["projected_cost_cny"], actual_cost_cny=run["actual_api_cost_cny"],
                   scope_id=scope["scope_id"], termination_reason=run["termination_reason"])
    _write_json(args.output, payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if run["status"] == "completed" else 1


def cmd_evolve(args) -> int:
    store = _store(args)
    engine = EvolutionFoundation(store)
    dataset = _load_dataset(args.dataset)
    _ensure_dataset(store, engine, dataset)
    val_ids, test_ids = _split_ids(dataset)
    runner = _build_runner(args)
    if args.protocol:
        protocol = ProtocolSpec.load(args.protocol)
        if (protocol.dataset_id != dataset["dataset_id"]
                or list(protocol.val_task_ids) != list(val_ids)
                or list(protocol.test_task_ids) != list(test_ids)
                or protocol.gate_profile not in (args.gate, "custom")):
            raise ValueError("导入协议与数据集划分/门禁档位不一致")
    else:
        protocol = build_protocol(dataset["dataset_id"], val_ids, test_ids,
                                  gate_profile=args.gate, repeats=args.repeats,
                                  seed=args.seed, confidence=args.confidence,
                                  min_sample=args.min_sample,
                                  protocol_id=args.protocol_id)
    snapshot = Path(args.protocol_out) if args.protocol_out else Path(args.state_dir)/"protocol.json"
    protocol.save(snapshot)
    if args.proposer == "reflective":
        if not hasattr(runner, "complete"):
            raise ValueError("reflective 提议器需要 --runner openai（LLM 客户端）")
        if not runner.api_key_value():
            raise ValueError(f"reflective 提议器缺少 API Key：请设置 {runner.api_key_env}")
        proposer = ReflectiveProposer(store, runner)
    else:
        proposer = MockProposer(store, args.strategy)
    result = engine.evolve(dataset["dataset_id"], val_ids, test_ids, proposer, runner,
                           gate_profile=args.gate, max_rounds=args.rounds, budget_cny=args.budget,
                           workers=args.workers, protocol=protocol, rate_limit=_rate_limit(args))
    payload = {k: result[k] for k in ("run_id", "status", "stop_reason", "gate_profile",
                                      "accepted", "rejected", "insufficient", "final_version",
                                      "val_metrics", "test_metrics", "cost_cny", "actual_cost_cny",
                                      "api_calls", "selection", "protocol_hash",
                                      "protocol_warnings")}
    payload["rounds"] = [{k: row.get(k) for k in ("round", "decision", "verdict", "reasons", "metrics")}
                         for row in result["rounds"]]
    payload["protocol_path"] = str(snapshot)
    payload["receipts"] = len(result["receipts"])
    _write_json(args.output, payload)
    if args.report:
        write_report(store, args.report, evolution_run_id=result["run_id"])
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_stats(args) -> int:
    store = _store(args)
    payload = EvolutionFoundation(store).stats(args.run)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_trace(args) -> int:
    store = _store(args)
    log = TraceLog(store.root/"trace"/"events.jsonl")
    if args.raw:
        for row in log.events(event=args.event, limit=args.limit):
            print(json.dumps(row, ensure_ascii=False))
        return 0
    summary = log.summary()
    if args.event:
        summary["by_event"] = {k: v for k, v in summary["by_event"].items() if k == args.event}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _latest_protocol(store, run_id=None) -> ProtocolSpec:
    records = store.list("evolution_runs")
    if run_id:
        record = store.read("evolution_runs", run_id)
    elif records:
        record = sorted(records, key=lambda item: item["updated_at"])[-1]
    else:
        raise KeyError("状态库里没有演化记录，无法导出协议；请用 --dataset 从数据集生成")
    if not record.get("protocol"):
        raise KeyError("该演化没有协议快照")
    return ProtocolSpec.from_dict(record["protocol"])


def cmd_protocol_export(args) -> int:
    store = _store(args)
    if args.dataset:
        dataset = _load_dataset(args.dataset)
        val_ids, test_ids = _split_ids(dataset)
        protocol = build_protocol(dataset["dataset_id"], val_ids, test_ids)
    else:
        protocol = _latest_protocol(store, args.run)
    protocol.save(args.out)
    print(json.dumps(dict(out=args.out, protocol_hash=protocol.hash,
                          warnings=protocol_warnings(protocol)), ensure_ascii=False, indent=2))
    return 0


def cmd_protocol_import(args) -> int:
    store = _store(args)
    protocol = ProtocolSpec.load(args.file)
    try:
        store.read("datasets", protocol.dataset_id)
        dataset = True
    except KeyError:
        dataset = False
    target = store.root/"protocol.json"
    protocol.save(target)
    print(json.dumps(dict(protocol_hash=protocol.hash, dataset_registered=dataset,
                          val_tasks=len(protocol.val_task_ids),
                          test_tasks=len(protocol.test_task_ids),
                          warnings=protocol_warnings(protocol)), ensure_ascii=False, indent=2))
    return 0


def cmd_protocol_show(args) -> int:
    store = _store(args)
    protocol = _latest_protocol(store, args.run)
    print(json.dumps(dict(protocol=protocol.to_dict(), protocol_hash=protocol.hash,
                          warnings=protocol_warnings(protocol)), ensure_ascii=False, indent=2))
    return 0


def cmd_report(args) -> int:
    store = _store(args)
    path = write_report(store, args.out, evolution_run_id=args.run)
    print(json.dumps({"report": str(path)}, ensure_ascii=False))
    return 0


def cmd_inspect(args) -> int:
    store = _store(args)
    print(json.dumps(EvolutionFoundation(store).resume(), ensure_ascii=False, indent=2))
    return 0


def cmd_receipts(args) -> int:
    store = _store(args)
    with store.connection() as conn:
        rows = EvolutionFoundation(store).receipts.all(conn)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cmd_verify(args) -> int:
    store = _store(args)
    with store.connection() as conn:
        try:
            result = verify_chain(conn)
        except IntegrityError as exc:
            print(json.dumps(dict(verified=False, error=str(exc)), ensure_ascii=False))
            return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_rollback(args) -> int:
    store = _store(args)
    try:
        store.rollback(args.version, args.reason, allow_simulated=args.allow_simulated)
    except (StateConflict, KeyError, IntegrityError) as exc:
        print(f"回滚失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps({"current": store.current()["version_id"]}, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, KeyError, StateConflict, IntegrityError, FileNotFoundError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
