"""复用官方四模块程序结构和模块反馈，连接 GEPA 原生优化器。"""

import ast
import concurrent.futures
import json
import threading
import time
from pathlib import Path

import dspy
from gepa.core.adapter import EvaluationBatch
from dspy.utils.exceptions import AdapterParseError
from model_client import GenerationTruncated
from scoring import f1_score, normalize


class HotpotMultiHop(dspy.Module):
    def __init__(self, retriever, direct_evidence=False):
        super().__init__()
        self.retriever = retriever
        self.direct_evidence = direct_evidence
        self.hop1_docs = []
        self.hop2_docs = []
        self.create_query_hop2 = dspy.ChainOfThought("question,summary_1->query")
        self.final_answer = dspy.ChainOfThought("question,summary_1,summary_2->answer")
        if direct_evidence:
            self.final_answer = dspy.ChainOfThought("question,summary_1,summary_2,evidence->answer")
        self.summarize1 = dspy.ChainOfThought("question,passages->summary")
        self.summarize2 = dspy.ChainOfThought("question,context,passages->summary")

    def forward(self, question):
        hop1_docs = self.retriever.search(question, k=7)
        self.hop1_docs = hop1_docs
        summary_1 = self.summarize1(question=question, passages=hop1_docs).summary
        query = self.create_query_hop2(question=question, summary_1=summary_1).query
        hop2_docs = self.retriever.search(query, k=7)
        self.hop2_docs = hop2_docs
        summary_2 = self.summarize2(question=question, context=summary_1, passages=hop2_docs).summary
        final_inputs = dict(question=question, summary_1=summary_1, summary_2=summary_2)
        if self.direct_evidence:
            # 保留两跳原始排序，去掉重复文档；不读取参考答案或支持标注。
            final_inputs['evidence'] = list(dict.fromkeys(hop1_docs + hop2_docs))
        answer = self.final_answer(**final_inputs).answer
        return dspy.Prediction(answer=answer, hop1_docs=hop1_docs, hop2_docs=hop2_docs,
                               summary_1=summary_1, summary_2=summary_2, hop2_query=query)


def load_official_feedback(root):
    # 仅加载原文件中的函数及反馈映射，避开模块导入时创建无关检索客户端。
    source = Path(root) / "upstream/gepa-artifact/gepa_artifact/benchmarks/hotpotQA/hotpot_program.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "feedback_fn_map" for t in node.targets))]
    namespace = {"dspy": dspy}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    # 当前 DSPy 将同名评分函数移动到 evaluate.metrics；只调整导入位置。
    from dspy.evaluate.metrics import EM, F1
    namespace["answer_match_fn"] = lambda prediction, answers, frac=1.0: EM(prediction, answers) if frac >= 1.0 else F1(prediction, answers) >= frac
    return namespace["feedback_fn_map"]


class HotpotAdapter:
    propose_new_texts = None

    def __init__(self, root, retriever, lm, client, run_dir, workers=4, direct_evidence=False):
        self.root, self.retriever, self.lm, self.client = root, retriever, lm, client
        self.path = Path(run_dir) / "rollouts.jsonl"
        self.workers = workers
        self.direct_evidence = direct_evidence
        self.lock = threading.Lock()
        self.feedback = load_official_feedback(root)
        self.completed = []
        if self.path.exists():
            self.completed = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def seed_candidate(self):
        return {name: pred.signature.instructions for name, pred in HotpotMultiHop(self.retriever).named_predictors()}

    def execute(self, row, candidate):
        self.client.local.example_id = row["id"]
        start = time.monotonic()
        program = HotpotMultiHop(self.retriever, direct_evidence=self.direct_evidence)
        for name, predictor in program.named_predictors():
            predictor.signature = predictor.signature.with_instructions(candidate[name])
        trace = []
        error = None
        try:
            with dspy.context(lm=self.lm, trace=trace):
                pred = program(question=row["question"])
        except (GenerationTruncated, AdapterParseError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            print(f"单题生成失败，按零分记录：{row['id']} {error}", flush=True)
            pred = dspy.Prediction(answer="", hop1_docs=program.hop1_docs, hop2_docs=program.hop2_docs)
        example = dspy.Example(**row).with_inputs("question")
        em = float(dspy.evaluate.answer_exact_match(example, pred))
        gold_titles = {title.strip() for title in row["supporting_facts"]["title"]}
        titles1 = {doc.split(" | ")[0].strip() for doc in pred.hop1_docs}
        titles2 = titles1 | {doc.split(" | ")[0].strip() for doc in pred.hop2_docs}
        names = {id(p): name for name, p in program.named_predictors()}
        steps = [{"component": names[id(p)], "inputs": inputs, "outputs": output.toDict()} for p, inputs, output in trace]
        result = {"id": row["id"], "split": row["split"], "phase": self.client.phase,
                  "candidate": candidate, "answer": pred.answer, "gold": row["answer"], "em": em,
                  "f1": f1_score(pred.answer, row["answer"]), "seconds": time.monotonic() - start,
                  "hop1_support_recall": len(gold_titles & titles1) / len(gold_titles),
                  "hop2_support_recall": len(gold_titles & titles2) / len(gold_titles),
                  "all_support_retrieved": float(gold_titles <= titles2),
                  "prediction": pred.toDict(), "steps": steps}
        if error:
            result["error"] = error
        with self.lock:
            self.completed.append(result)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(result, ensure_ascii=False) + "\n")
            total = len(self.completed)
        if total % 10 == 0:
            print(f"已完成 {total} 条轨迹，当前阶段 {self.client.phase}，累计模型费估算 {self.client.spent:.3f} 元", flush=True)
        return result

    def evaluate(self, batch, candidate, capture_traces=False):
        # 验证集已完成的同候选输出可跨运行恢复，保持初始基线不重复抽样。
        previous = {r["id"]: r for r in self.completed if r["split"] == "val" and r["candidate"] == candidate}
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
            results = list(pool.map(lambda row: previous[row["id"]] if row["split"] == "val" and row["id"] in previous else self.execute(row, candidate), batch))
        return EvaluationBatch(outputs=results, scores=[r["em"] for r in results],
                               trajectories=[{"row": row, "result": r} for row, r in zip(batch, results)] if capture_traces else None)

    def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
        output = {name: [] for name in components_to_update}
        for trajectory in eval_batch.trajectories:
            row, result = trajectory["row"], trajectory["result"]
            example = dspy.Example(**row).with_inputs("question")
            pred = dspy.Prediction(**result["prediction"])
            for name in components_to_update:
                if result.get("error"):
                    output[name].append({"Inputs": {"question": row["question"], "completed_steps": result["steps"]},
                        "Generated Outputs": result["prediction"],
                        "Feedback": f"The pipeline failed and scored 0. Error: {result['error']}. Correct answer: {row['answer']}. Supporting facts: {row['supporting_facts']}. Ensure outputs follow the required field format and finish within the generation budget."})
                    continue
                step = next(s for s in result["steps"] if s["component"] == name)
                feedback = self.feedback[name](predictor_output=step["outputs"], predictor_inputs=step["inputs"],
                    module_inputs=example, module_outputs=pred, captured_trace=[])
                output[name].append({"Inputs": step["inputs"], "Generated Outputs": step["outputs"],
                                     "Feedback": feedback["feedback_text"]})
        return output
