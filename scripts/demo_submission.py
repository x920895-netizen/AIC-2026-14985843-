"""参赛离线功能演示：合成任务只验证门禁控制流，不表示模型准确率。"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("DSPY_CACHEDIR", str(ROOT / ".cache" / "dspy"))
from harness import CandidateProposal, EvolutionFoundation, FoundationStore, GateSpec, RuntimeTask, TargetKind
from harness.storage import StateConflict


def require_dspy():
    """演示也需要独立评价器；缺依赖时给出可读指引，而不是在断言处失败。"""
    try:
        import dspy  # noqa: F401
    except ModuleNotFoundError:
        sys.exit("缺少 dspy==3.4.1：请先执行 `pip install -e \".[research]\"`（或 `pip install dspy==3.4.1`）再重试。"
                 "本演示不需要 API Key，也不会发起网络请求。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="新的演示目录，不覆盖历史记录")
    args = parser.parse_args()
    require_dspy()
    args.output.mkdir(parents=True, exist_ok=False)
    store = FoundationStore(args.output / "state")
    engine = EvolutionFoundation(store)
    artifacts = {
        "prompt": {"components": {"final_answer.predict": "合成演示初始策略"}},
        "memory": {"experience_ids": [], "retrieval_policy": {}, "lifecycle_policy": {}},
        "tool": {"tools": [], "selection_policy": {}, "risk_constraints": {}},
        "skill": {"definitions": [], "selection_policy": {}, "lifecycle_policy": {}},
    }
    refs = {k: store.write_artifact(k, v) for k, v in artifacts.items()}
    config = {"model_config": {"model": "offline-deterministic", "temperature": 0}}
    base = store.initialize(refs, config, experiment_config={"study_id": "submission-synthetic-demo"})
    tasks = [RuntimeTask(f"demo-{i}", f"本地合成问题{i}") for i in range(4)]
    labels = {t.task_id: f"answer {i}" for i, t in enumerate(tasks)}
    engine.register_dataset("synthetic-demo", tasks, labels)
    spec = GateSpec("synthetic-demo", tuple(t.task_id for t in tasks), config["model_config"])
    events = []

    def event(name, **values):
        row = dict(event=name, **values)
        events.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    def candidate(instruction):
        parent = store.current()
        ref = store.write_artifact("prompt", {"components": {"final_answer.predict": instruction}})
        return engine.register(CandidateProposal(parent["version_id"], TargetKind.PROMPT, ref, spec,
            "仅用于离线功能演示的人工预置候选", expected_benefit="验证控制流", risks=("合成数据无模型效果含义",)))

    def pair(c, old_correct, new_correct):
        scope = engine.budget.create(1.0)
        def runner(correct):
            # 输出按任务ID预置，评分标签仍由独立评价器读取。
            return lambda task, version, artifacts: f"answer {task.task_id.split('-')[-1]}" if correct else "incorrect"
        old = engine.run(c["parent_version_id"], spec, scope["scope_id"], runner(old_correct), projected_cost_per_task=0.001)
        new = engine.run(c["version_id"], spec, scope["scope_id"], runner(new_correct),
                         candidate_id=c["candidate_id"], projected_cost_per_task=0.001)
        return engine.validate(c["candidate_id"], old["run_id"], new["run_id"])

    event("初始化", version=base["version_id"], mode="simulation", api_calls=0)
    good = candidate("合成演示可接受策略")
    report = pair(good, False, True)
    assert report["passed"] and store.current()["version_id"] == base["version_id"]
    event("独立验证通过，当前版本尚未切换", passed=True, metrics=report["metrics"], diff=good["change"]["diff"])
    accepted = engine.accept(good["candidate_id"])
    event("原子接受", current=accepted["version_id"], parent=base["version_id"])
    bad = candidate("合成演示退化策略")
    rejected = pair(bad, True, False)
    assert not rejected["passed"]
    try:
        engine.accept(bad["candidate_id"])
        raise AssertionError("退化候选不应被接受")
    except StateConflict:
        pass
    assert store.current()["version_id"] == accepted["version_id"]
    event("退化候选被拒绝，当前版本保留", reasons=rejected["reasons"], metrics=rejected["metrics"])
    reopened = EvolutionFoundation(FoundationStore(args.output / "state")).resume()
    assert reopened["current_version"]["version_id"] == accepted["version_id"]
    event("重新打开持久状态", restored=True, candidates=len(reopened["candidates"]))
    store.rollback(base["version_id"], "参赛离线演示的显式回滚")
    assert store.current()["version_id"] == base["version_id"]
    event("显式回滚至历史祖先", restored=True, current=base["version_id"])
    result = dict(mode="simulation", fixture="synthetic", api_calls=0, actual_api_cost_cny=0,
                  automated_candidate_generation=False, events=events, passed=True)
    (args.output / "demo.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
