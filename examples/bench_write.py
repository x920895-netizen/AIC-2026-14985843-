"""SQLite 写入压测：WAL、单写多读与并发写吞吐。

用与线上执行器相同的写路径（``transaction`` + ``put``，一次事务落一条输出）
压 1000 条输出，输出吞吐与分位延迟。不联网；耗时主要由磁盘同步（WAL +
``synchronous=FULL``）决定，本机约 30–60 秒。

    python examples/bench_write.py
    python examples/bench_write.py --records 2000 --workers 8 --json

三个测量项：

1. ``append``：单写者逐条追加（与 ``OnlineExecutor`` 每完成一题落盘一致）；
2. ``concurrent``：多写者各写自己的记录（WAL 下由 busy_timeout 排队）；
3. ``read-during-write``：写者持续写入时读者反复打开连接读取，验证读不被写阻塞。
"""

import argparse
import json
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.storage import FoundationStore  # noqa: E402

COLLECTION = "bench_outputs"


def percentile(values, q):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered)-1, max(0, int(round(q*(len(ordered)-1)))))
    return ordered[index]


def bench_append(store, records):
    """单写者：每条输出一个事务，形状与执行器落盘一致。"""
    latencies = []
    began = time.perf_counter()
    for index in range(records):
        value = dict(task_id=f"task-{index:05d}", repeat=1, status="completed",
                     answer=f"answer {index}", prompt_tokens=12, completion_tokens=4,
                     cost_cny=0.00001*index)
        tick = time.perf_counter()
        with store.transaction("bench.append") as conn:
            store.put(conn, COLLECTION, f"row-{index:05d}", value)
        latencies.append((time.perf_counter()-tick)*1000)
    elapsed = time.perf_counter()-began
    return dict(records=records, elapsed_s=elapsed, throughput=records/elapsed,
                latency_ms=dict(p50=percentile(latencies, 0.50),
                                p95=percentile(latencies, 0.95),
                                mean=statistics.fmean(latencies)))


def bench_concurrent(store, records, workers):
    """多写者：每个线程写自己的记录，测聚合吞吐与锁冲突。"""
    per_worker = max(1, records//workers)
    errors = []
    latencies = []
    lock = threading.Lock()

    def worker(index):
        try:
            for step in range(per_worker):
                value = dict(worker=index, step=step, status="completed")
                tick = time.perf_counter()
                with store.transaction("bench.concurrent") as conn:
                    store.put(conn, COLLECTION, f"w{index}-{step:05d}", value)
                with lock:
                    latencies.append((time.perf_counter()-tick)*1000)
        except Exception as exc:  # 记录失败原因，不让线程静默退出
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    began = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter()-began
    total = per_worker*workers
    return dict(workers=workers, records=total, elapsed_s=elapsed,
                throughput=total/elapsed if elapsed else 0.0,
                latency_ms=dict(p50=percentile(latencies, 0.50),
                                p95=percentile(latencies, 0.95)),
                errors=errors)


def bench_read_during_write(store, records, reads=200):
    """写者持续写入时读者反复读取：WAL 下读不应被阻塞。"""
    stop = threading.Event()
    written = {"n": 0}

    def writer():
        index = 0
        while not stop.is_set():
            with store.transaction("bench.background") as conn:
                store.put(conn, COLLECTION, f"bg-{index:06d}", dict(index=index))
            written["n"] = index
            index += 1
            if index >= records:
                break

    def reader():
        latencies = []
        errors = []
        for _ in range(reads):
            tick = time.perf_counter()
            try:
                with store.connection() as conn:
                    conn.execute("SELECT count(*) FROM documents").fetchone()
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
            latencies.append((time.perf_counter()-tick)*1000)
        return latencies, errors

    thread = threading.Thread(target=writer)
    began = time.perf_counter()
    thread.start()
    latencies, errors = reader()
    stop.set()
    thread.join()
    elapsed = time.perf_counter()-began
    return dict(reads=reads, written=written["n"], elapsed_s=elapsed,
                latency_ms=dict(p50=percentile(latencies, 0.50),
                                p95=percentile(latencies, 0.95)),
                errors=errors)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--records", type=int, default=1000, help="追加记录数（默认 1000）")
    parser.add_argument("--workers", type=int, default=8, help="并发写线程数（默认 8）")
    parser.add_argument("--json", action="store_true", help="只输出一行 JSON 摘要")
    parser.add_argument("--state", type=Path, default=None, help="状态目录；默认临时目录")
    args = parser.parse_args(argv)
    if args.records < 1:
        raise SystemExit("--records 必须为正整数")
    if args.workers < 1:
        raise SystemExit("--workers 必须为正整数")

    temp = None
    root = args.state
    if root is None:
        temp = tempfile.TemporaryDirectory(prefix="bench-write-")
        root = Path(temp.name)/"state"
    store = FoundationStore(root)
    with store.connection() as conn:
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        sync = conn.execute("PRAGMA synchronous").fetchone()[0]

    append = bench_append(store, args.records)
    concurrent = bench_concurrent(store, args.records, args.workers)
    readers = bench_read_during_write(store, max(1, args.records//10),
                                      reads=max(50, args.records//5))
    with store.connection() as conn:
        total_rows = conn.execute("SELECT count(*) FROM documents WHERE collection=?",
                                  (COLLECTION,)).fetchone()[0]
    duration = append["elapsed_s"]+concurrent["elapsed_s"]+readers["elapsed_s"]
    summary = dict(records=args.records, journal_mode=str(journal).upper(),
                   synchronous=str(sync), total_rows=total_rows,
                   append=append, concurrent=concurrent, read_during_write=readers,
                   ok=not concurrent["errors"] and not readers["errors"],
                   total_elapsed_s=duration, state=str(root))
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print(f"SQLite 写入压测（journal_mode={summary['journal_mode']}，"
              f"synchronous={summary['synchronous']}，记录数 {args.records}）\n")
        print("| 场景 | 记录数 | 耗时(s) | 吞吐(条/s) | p50(ms) | p95(ms) | 错误 |")
        print("| --- | ---: | ---: | ---: | ---: | ---: | --- |")
        print(f"| 单写者逐条追加 | {append['records']} | {append['elapsed_s']:.3f} | "
              f"{append['throughput']:.0f} | {append['latency_ms']['p50']:.3f} | "
              f"{append['latency_ms']['p95']:.3f} | — |")
        print(f"| 并发写({concurrent['workers']} 线程) | {concurrent['records']} | "
              f"{concurrent['elapsed_s']:.3f} | {concurrent['throughput']:.0f} | "
              f"{concurrent['latency_ms']['p50']:.3f} | {concurrent['latency_ms']['p95']:.3f} | "
              f"{len(concurrent['errors'])} |")
        print(f"| 写时读(WAL) | {readers['reads']} 次读 | {readers['elapsed_s']:.3f} | — | "
              f"{readers['latency_ms']['p50']:.3f} | {readers['latency_ms']['p95']:.3f} | "
              f"{len(readers['errors'])} |")
        print(f"\n库内文档总数：{total_rows}；总耗时 {duration:.3f} 秒；状态目录 {root}")
        if concurrent["errors"] or readers["errors"]:
            print("失败样例："+"; ".join((concurrent["errors"]+readers["errors"])[:3]))
            return 1
    if temp is not None:
        temp.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
