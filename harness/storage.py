"""本地SQLite事务状态与原子工件文件；保护既有实验目录。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from .contracts import EVALUATOR_IDS, TargetKind, canonical, content_hash, new_id, now, validate_artifact


class IntegrityError(RuntimeError):
    pass


class StateConflict(RuntimeError):
    pass


class FoundationStore:
    def __init__(self, root: str | Path, fault_hook: Callable[[str], None] | None = None):
        self.root = Path(root).resolve()
        project = Path(__file__).resolve().parents[1]
        if self.root == project or any(self.root == project/name or project/name in self.root.parents
                                      for name in ["runs", "data", "upstream", "scripts"]):
            raise ValueError("公共状态须使用独立目录，不能写入Baseline、数据或历史实验目录")
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root/"artifacts").mkdir(exist_ok=True)
        self.db_path = self.root/"foundation.sqlite3"
        self.fault_hook = fault_hook
        with self.connection() as conn:
            # WAL：并发读不阻塞写，写入者之间由 busy_timeout 排队；
            # 该设置持久化在数据库文件里，重开仍是 WAL。
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
            if mode is None or str(mode[0]).lower() != "wal":
                raise RuntimeError("无法启用 SQLite WAL 模式")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                    collection TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
                    digest TEXT NOT NULL, PRIMARY KEY(collection,id));
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, receipt_id TEXT NOT NULL UNIQUE,
                    candidate_id TEXT NOT NULL, decision TEXT NOT NULL,
                    payload TEXT NOT NULL, digest TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS receipts_candidate_decision
                    ON receipts(candidate_id, decision);
            """)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self, operation: str):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                self.checkpoint(operation+".before_commit")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def checkpoint(self, name: str):
        if self.fault_hook is not None:
            self.fault_hook(name)

    def put(self, conn, collection: str, key: str, value: dict, *, insert=False):
        payload = canonical(value).decode("utf-8")
        if insert:
            conn.execute("INSERT INTO documents VALUES(?,?,?,?)", (collection, key, payload, content_hash(value)))
        else:
            conn.execute("INSERT INTO documents VALUES(?,?,?,?) ON CONFLICT(collection,id) "
                         "DO UPDATE SET payload=excluded.payload,digest=excluded.digest",
                         (collection, key, payload, content_hash(value)))

    def get(self, conn, collection: str, key: str) -> dict:
        row = conn.execute("SELECT payload,digest FROM documents WHERE collection=? AND id=?", (collection, key)).fetchone()
        if row is None:
            raise KeyError(f"没有{collection}/{key}")
        try:
            value = json.loads(row["payload"])
            if content_hash(value) != row["digest"]:
                raise IntegrityError(f"状态记录完整性不符：{collection}/{key}")
            return value
        except (ValueError, TypeError) as exc:
            raise IntegrityError(f"状态记录无法读取：{collection}/{key}") from exc

    def read(self, collection: str, key: str) -> dict:
        with self.connection() as conn:
            return self.get(conn, collection, key)

    def list(self, collection: str) -> list[dict]:
        with self.connection() as conn:
            keys = [r[0] for r in conn.execute("SELECT id FROM documents WHERE collection=? ORDER BY id", (collection,))]
            return [self.get(conn, collection, key) for key in keys]

    def event(self, conn, kind: str, payload: dict):
        conn.execute("INSERT INTO events(kind,payload,created_at) VALUES(?,?,?)",
                     (kind, canonical(payload).decode("utf-8"), now()))

    def events(self) -> list[dict]:
        with self.connection() as conn:
            return [dict(seq=r["seq"], kind=r["kind"], payload=json.loads(r["payload"]), created_at=r["created_at"])
                    for r in conn.execute("SELECT * FROM events ORDER BY seq")]

    def write_artifact(self, kind: TargetKind | str, content: dict) -> dict:
        kind = TargetKind(kind).value
        validate_artifact(kind, content)
        artifact_id = new_id("artifact")
        path = self.root/"artifacts"/(artifact_id+".json")
        data = canonical(dict(kind=kind, content=content))
        temporary = path.with_suffix(".tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return dict(artifact_id=artifact_id, kind=kind, path=path.relative_to(self.root).as_posix(),
                    content_hash=hashlib.sha256(data).hexdigest())

    def load_artifact(self, ref: dict) -> dict:
        try:
            path = (self.root/ref["path"]).resolve()
            if self.root/"artifacts" not in path.parents:
                raise IntegrityError("工件路径越出状态工件目录")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != ref["content_hash"]:
                raise IntegrityError(f"工件哈希不匹配：{ref['artifact_id']}")
            value = json.loads(data)
            if value["kind"] != ref["kind"]:
                raise IntegrityError("工件类型不匹配")
            validate_artifact(value["kind"], value["content"])
            return value["content"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise IntegrityError("工件缺失、损坏或schema无效") from exc

    @staticmethod
    def seal_version(version: dict) -> dict:
        version = dict(version)
        version.pop("content_hash", None)
        return dict(version, content_hash=content_hash(version))

    def get_version(self, conn, version_id: str, *, verify_artifacts=True) -> dict:
        version = self.get(conn, "versions", version_id)
        payload = {k: v for k, v in version.items() if k != "content_hash"}
        if content_hash(payload) != version["content_hash"]:
            raise IntegrityError("版本内容哈希不匹配")
        if set(version["artifacts"]) != {k.value for k in TargetKind}:
            raise IntegrityError("版本缺少四类工件引用")
        for kind, ref in version["artifacts"].items():
            if ref["kind"] != kind:
                raise IntegrityError("版本与工件类型不一致")
            if verify_artifacts:
                self.load_artifact(ref)
        return version

    def version(self, version_id: str) -> dict:
        with self.connection() as conn:
            return self.get_version(conn, version_id)

    def current_id(self, conn) -> str:
        row = conn.execute("SELECT value FROM meta WHERE key='current_version'").fetchone()
        if row is None:
            raise StateConflict("尚未创建初始版本")
        return row[0]

    def current(self) -> dict:
        with self.connection() as conn:
            return self.get_version(conn, self.current_id(conn))

    def check_memory(self, conn, version: dict, *, allow_simulated: bool):
        memory = self.load_artifact(version["artifacts"]["memory"])
        for exp_id in memory["experience_ids"]:
            exp = self.get(conn, "experiences", exp_id)
            if exp["status"] != "verified" or (not allow_simulated and exp["verification_level"] != "real"):
                raise StateConflict(f"经验不可用于恢复/发布：{exp_id}")

    def initialize(self, artifacts: dict, config_snapshot: dict, *, experiment_config: dict | None = None,
                   evaluator_id: str = "hotpot-em-f1-v1") -> dict:
        if evaluator_id not in EVALUATOR_IDS:
            raise ValueError("未知评价协议；公共门禁不接受调用方自定义打分器")
        if set(artifacts) != {x.value for x in TargetKind}:
            raise ValueError("初始版本必须引用全部四类工件")
        if not isinstance(config_snapshot.get("model_config"), dict):
            raise ValueError("配置快照必须包含model_config")
        if any(ref["kind"] != kind for kind, ref in artifacts.items()):
            raise ValueError("初始工件类型与引用槽位不一致")
        decision = dict(decision_id=new_id("decision"), candidate_id=None, evaluation_id=None,
            verdict="accepted", mode="offline_bootstrap", created_at=now(),
            reasons=["显式登记初始基线版本；未声明经过候选性能验证"])
        version = self.seal_version(dict(version_id=new_id("version"), parent_version_id=None,
            created_at=now(), status="accepted", artifacts=artifacts, config_snapshot=config_snapshot,
            evaluator_id=evaluator_id, experiment_config=experiment_config or {}, decision_ids=[decision["decision_id"]]))
        with self.transaction("initialize") as conn:
            if conn.execute("SELECT 1 FROM meta WHERE key='current_version'").fetchone():
                raise StateConflict("已有初始版本，不允许覆盖")
            for ref in artifacts.values():
                self.load_artifact(ref)
            self.check_memory(conn, version, allow_simulated=False)
            self.put(conn, "versions", version["version_id"], version, insert=True)
            self.put(conn, "decisions", decision["decision_id"], decision, insert=True)
            conn.execute("INSERT INTO meta VALUES('current_version',?)", (version["version_id"],))
            self.event(conn, "initialized", dict(version_id=version["version_id"]))
        return version

    def rollback(self, version_id: str, reason: str, *, allow_simulated=False) -> dict:
        if not reason:
            raise ValueError("回滚必须记录原因")
        with self.transaction("rollback") as conn:
            current_id = self.current_id(conn)
            target = self.get_version(conn, version_id)
            if target["status"] != "accepted":
                raise StateConflict("只能恢复历史已接受版本")
            cursor = self.get_version(conn, current_id, verify_artifacts=False)
            ancestors = {cursor["version_id"]}
            while cursor["parent_version_id"]:
                cursor = self.get_version(conn, cursor["parent_version_id"], verify_artifacts=False)
                ancestors.add(cursor["version_id"])
            if version_id not in ancestors:
                raise StateConflict("回滚目标必须属于当前版本祖先链")
            self.check_memory(conn, target, allow_simulated=allow_simulated)
            conn.execute("UPDATE meta SET value=? WHERE key='current_version'", (version_id,))
            self.event(conn, "rollback", dict(from_version=current_id, to_version=version_id, reason=reason))
        return target
