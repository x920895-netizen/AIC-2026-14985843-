"""下载官方数据，固定与论文母划分一致的 30/30/30 子集。"""

import concurrent.futures
import json
import os
import random
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def download(url, target):
    target = Path(target)
    if target.exists():
        print(f"已存在：{target.name}", flush=True)
        return
    partial = target.with_suffix(target.suffix + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(4):
        offset = partial.stat().st_size if partial.exists() else 0
        try:
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            # 时间戳避免代理复用已经失效的下载重定向。
            request_url = url + ("&" if "?" in url else "?") + f"download=true&t={int(time.time())}"
            with requests.get(request_url, headers=headers, stream=True, timeout=(30, 120)) as response:
                response.raise_for_status()
                append = offset > 0 and response.status_code == 206
                expected = int(response.headers.get("Content-Length", 0))
                received = 0
                last = time.monotonic()
                with partial.open("ab" if append else "wb") as handle:
                    for chunk in response.iter_content(1024 * 1024):
                        handle.write(chunk)
                        received += len(chunk)
                        if time.monotonic() - last > 20:
                            print(f"下载 {target.name}：{handle.tell() / 1e6:.0f} MB", flush=True)
                            last = time.monotonic()
                if expected and received != expected:
                    raise OSError(f"文件传输长度不符：{received}/{expected}")
            os.replace(partial, target)
            print(f"下载完成：{target.name}，{target.stat().st_size / 1e6:.1f} MB", flush=True)
            return
        except (requests.RequestException, OSError) as exc:
            print(f"下载失败，第 {attempt + 1} 次：{target.name} {exc}", flush=True)
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))


def make_splits():
    import pyarrow.parquet as pq

    target = DATA / "splits.json"
    if target.exists():
        return
    tables = [pq.read_table(DATA / f"train-{i:05d}-of-00002.parquet") for i in range(2)]
    count = sum(t.num_rows for t in tables)
    ranges = {"train": (int(count * 0.8), count), "val": (int(count * 0.4), int(count * 0.8)), "test": (0, int(count * 0.4))}
    result = {}
    for split, (start, end) in ranges.items():
        indices = random.Random(1).sample(range(start, end), 30)
        rows = []
        for index in indices:
            table = tables[0] if index < tables[0].num_rows else tables[1]
            local_index = index if index < tables[0].num_rows else index - tables[0].num_rows
            row = table.slice(local_index, 1).to_pylist()[0]
            row["source_index"] = index
            row["split"] = split
            rows.append(row)
        result[split] = rows
    ids = [row["id"] for rows in result.values() for row in rows]
    assert len(ids) == len(set(ids)) == 90
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    metadata = {"source": "hotpotqa/hotpot_qa/fullwiki/train", "source_rows": count, "seed": 1,
                "parent_ranges": ranges, "ids": {s: [r["id"] for r in rows] for s, rows in result.items()},
                "types": {s: {t: sum(r["type"] == t for r in rows) for t in {r["type"] for r in rows}} for s, rows in result.items()}}
    (DATA / "split_manifest.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"数据划分完成：{metadata['types']}", flush=True)


def main():
    DATA.mkdir(exist_ok=True)
    downloads = [
        ("https://huggingface.co/dspy/cache/resolve/main/wiki.abstracts.2017.tar.gz", DATA / "wiki.abstracts.2017.tar.gz"),
        *[(f"https://huggingface.co/datasets/hotpotqa/hotpot_qa/resolve/main/fullwiki/train-{i:05d}-of-00002.parquet", DATA / f"train-{i:05d}-of-00002.parquet") for i in range(2)],
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda item: download(*item), downloads))
    make_splits()


if __name__ == "__main__":
    main()
