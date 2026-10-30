"""沿用官方 Wikipedia 2017 摘要与 BM25 参数。"""

import gc
import json
import tarfile
from functools import lru_cache
from pathlib import Path

import bm25s
import Stemmer


class Retriever:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.stemmer = Stemmer.Stemmer("english")
        index = self.data_dir / "bm25_index"
        corpus_path = self.data_dir / "wiki.abstracts.2017.jsonl"
        if not corpus_path.exists():
            print("正在解压 Wikipedia 2017 摘要", flush=True)
            with tarfile.open(self.data_dir / "wiki.abstracts.2017.tar.gz", "r:gz") as archive:
                member = next(m for m in archive.getmembers() if Path(m.name).name == corpus_path.name)
                with archive.extractfile(member) as source, corpus_path.open("wb") as target:
                    import shutil
                    shutil.copyfileobj(source, target)
        print("正在读取完整 Wikipedia 摘要语料", flush=True)
        self.corpus = []
        with corpus_path.open(encoding="utf-8") as handle:
            for line in handle:
                doc = json.loads(line)
                self.corpus.append(f"{doc['title']} | {' '.join(doc['text'])}")
        print(f"语料数量：{len(self.corpus):,}", flush=True)
        if not (index / "params.index.json").exists():
            print("正在建立完整 BM25 索引，k1=0.9，b=0.4", flush=True)
            tokens = bm25s.tokenize(self.corpus, stopwords="en", stemmer=self.stemmer)
            self.engine = bm25s.BM25(k1=0.9, b=0.4)
            self.engine.index(tokens)
            self.engine.save(str(index))
            del tokens
            gc.collect()
        else:
            self.engine = bm25s.BM25.load(str(index), mmap=True)
        print("检索索引就绪", flush=True)

    @lru_cache(maxsize=4096)
    def search(self, query, k=7):
        tokens = bm25s.tokenize(query, stopwords="en", stemmer=self.stemmer, show_progress=False)
        results, _ = self.engine.retrieve(tokens, k=k, n_threads=1, show_progress=False)
        return [self.corpus[int(doc)] for doc in results[0]]
