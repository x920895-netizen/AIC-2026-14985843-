"""带持久化用量账本和并发费用预留的模型客户端。"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import dspy
from openai import APIConnectionError, OpenAI, RateLimitError
from usage import estimate_reservation


class BudgetExceeded(RuntimeError):
    pass


class GenerationTruncated(RuntimeError):
    pass


def load_usage_records(run_dir):
    run_dir = Path(run_dir)
    records = []
    calibration = run_dir.parent / "round1-calibration-2048/api_calls.jsonl"
    if calibration.exists():
        for line in calibration.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            row["phase"] = "calibration_2048_" + row["phase"]
            records.append(row)
    current = run_dir / "api_calls.jsonl"
    if current.exists():
        records.extend(json.loads(line) for line in current.read_text(encoding="utf-8").splitlines())
    return records


class ModelClient:
    def __init__(self, config, run_dir):
        self.config = config
        self.path = Path(run_dir) / "api_calls.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.local = threading.local()
        self.reserved = 0.0
        records = load_usage_records(run_dir)
        self.spent = sum(row["cost_cny"] for row in records)
        self.reflection_cache = {json.dumps(row["messages"], ensure_ascii=False, sort_keys=True): row["response"]["choices"][0]["message"]["content"]
            for row in records if row["role"] == "reflection" and not row.get("error")}
        self.phase = "setup"
        self.timeout_seconds = 600
        self.api = OpenAI(base_url=config["base_url"], api_key=os.environ["DASHSCOPE_API_KEY"], max_retries=0, timeout=self.timeout_seconds)

    def call(self, role, messages, **kwargs):
        for attempt in range(4):
            try:
                return self._call_once(role, messages, **kwargs)
            except (RateLimitError, APIConnectionError) as exc:
                if attempt == 3:
                    raise
                retry_after = exc.response.headers.get("retry-after") if isinstance(exc, RateLimitError) else None
                delay = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else 15 * (2 ** attempt)
                reason = "服务限流" if isinstance(exc, RateLimitError) else "连接中断或超时"
                print(f"{role} {reason}，第 {attempt + 1}/3 次退避，等待 {delay:.0f} 秒后重试", flush=True)
                time.sleep(delay)

    def _call_once(self, role, messages, **kwargs):
        model = self.config[f"{role}_model"]
        prices = self.config[f"{role}_prices_cny_per_million"]
        max_tokens = self.config[f"{role}_max_tokens"]
        # UTF-8 字节数加消息封装余量用于保守预留；不依赖未核实的模型分词器。
        reservation = estimate_reservation(messages, max_tokens, prices)
        with self.lock:
            if self.spent + self.reserved + reservation > self.config["budget_cny"]:
                raise BudgetExceeded(f"费用额度不足：已记账 {self.spent:.4f} 元，在途 {self.reserved:.4f} 元，本次预留 {reservation:.4f} 元")
            self.reserved += reservation
        start = time.monotonic()
        record = {"time": datetime.now(timezone.utc).isoformat(), "phase": self.phase,
                  "example_id": getattr(self.local, "example_id", None), "role": role, "model": model,
                  "messages": messages, "reserved_cny": reservation, "max_tokens": max_tokens,
                  "enable_thinking": self.config[f"{role}_enable_thinking"], "timeout_seconds": self.timeout_seconds}
        cost = reservation
        try:
            parameters = {"model": model, "messages": messages, "max_tokens": max_tokens,
                          "temperature": 0.0 if role == "task" else 1.0,
                          "extra_body": {"enable_thinking": self.config[f"{role}_enable_thinking"]}}
            if kwargs.get("response_format"):
                parameters["response_format"] = kwargs["response_format"]
            response = self.api.chat.completions.create(**parameters)
            if response.usage is None:
                raise RuntimeError("API 没有返回用量，保留本次完整预留费用并停止")
            usage = response.usage
            cost = (usage.prompt_tokens * prices[0] + usage.completion_tokens * prices[1]) / 1e6
            record.update({"usage": usage.model_dump(), "response": response.model_dump(), "cost_kind": "按返回用量估算"})
            if response.choices[0].finish_reason == "length":
                raise GenerationTruncated(f"{role} 输出达到 {max_tokens} Token 上限，finish_reason=length")
            return response
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            record.setdefault("cost_kind", "失败请求按预留上界记账")
            raise
        finally:
            record["seconds"] = time.monotonic() - start
            record["cost_cny"] = cost
            with self.lock:
                self.reserved -= reservation
                self.spent += cost
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def reflect(self, prompt):
        messages = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else prompt
        key = json.dumps(messages, ensure_ascii=False, sort_keys=True)
        if key in self.reflection_cache:
            print("复用同一已完成反思请求的输出", flush=True)
            return self.reflection_cache[key]
        response = self.call("reflection", messages)
        self.reflection_cache[key] = response.choices[0].message.content
        return self.reflection_cache[key]


class TaskLM(dspy.BaseLM):
    def __init__(self, client):
        super().__init__(model=client.config["task_model"], temperature=0.0,
                         max_tokens=client.config["task_max_tokens"], cache=False, num_retries=0)
        self.client = client

    def forward(self, prompt=None, messages=None, **kwargs):
        messages = messages or [{"role": "user", "content": prompt}]
        return self.client.call("task", messages, **kwargs)
