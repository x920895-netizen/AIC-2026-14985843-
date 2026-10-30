"""决策凭证：每一次 accept / reject 生成一条可链式复核的 SHA-256 凭证。

凭证只记录“结论及其证据指纹”，不复制原始输出。任何人拿到 SQLite 状态文件，
都能用 :func:`verify_chain` 复核：字段是否完整、哈希是否被改、链是否断开。
"""

import hashlib
import json

from .contracts import GateSpec, canonical, content_hash, new_id, now
from .storage import IntegrityError, StateConflict

GENESIS = ""

#: 凭证固定字段（顺序不影响哈希；集合用于稳定性校验）。
RECEIPT_FIELDS = (
    "receipt_id", "created_at", "decision", "candidate_id", "parent_version",
    "candidate_version", "profile", "gate_spec_hash", "protocol_hash", "diff_hash",
    "metrics_hash", "evidence_hash", "prev_receipt_hash", "receipt_hash",
)

DECISIONS = {"accept", "reject", "insufficient"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id TEXT NOT NULL UNIQUE,
    candidate_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    payload TEXT NOT NULL,
    digest TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS receipts_candidate_decision
    ON receipts(candidate_id, decision);
"""


def receipt_hash(receipt: dict) -> str:
    """对除 ``receipt_hash`` 自身以外的字段做规范化 JSON 的 SHA-256。"""
    body = {k: v for k, v in receipt.items() if k != "receipt_hash"}
    return hashlib.sha256(canonical(body)).hexdigest()


def make_receipt(*, decision: str, candidate_id: str, parent_version: str,
                 candidate_version: str, profile: str, gate_spec_hash: str,
                 diff_hash: str, metrics_hash: str, evidence_hash: str,
                 protocol_hash: str = "",
                 prev_receipt_hash: str = GENESIS, created_at: str | None = None,
                 receipt_id: str | None = None) -> dict:
    if decision not in DECISIONS:
        raise ValueError("凭证决策只能是 accept、reject 或 insufficient")
    receipt = dict(
        receipt_id=receipt_id or new_id("receipt"),
        created_at=created_at or now(),
        decision=decision,
        candidate_id=candidate_id,
        parent_version=parent_version,
        candidate_version=candidate_version,
        profile=profile,
        gate_spec_hash=gate_spec_hash,
        protocol_hash=protocol_hash or "",
        diff_hash=diff_hash,
        metrics_hash=metrics_hash,
        evidence_hash=evidence_hash,
        prev_receipt_hash=prev_receipt_hash,
    )
    receipt["receipt_hash"] = receipt_hash(receipt)
    return receipt


def verify_chain(conn) -> dict:
    """按写入顺序复核凭证链；任何篡改、断链或缺字段都抛 ``IntegrityError``。"""
    rows = conn.execute("SELECT seq, payload, digest FROM receipts ORDER BY seq").fetchall()
    previous = GENESIS
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError) as exc:
            raise IntegrityError(f"凭证无法读取：seq={row['seq']}") from exc
        if set(payload) != set(RECEIPT_FIELDS):
            raise IntegrityError(f"凭证字段集合不符：seq={row['seq']}")
        if receipt_hash(payload) != payload["receipt_hash"]:
            raise IntegrityError(f"凭证哈希不匹配（内容被改动）：seq={row['seq']}")
        if payload["receipt_hash"] != row["digest"]:
            raise IntegrityError(f"凭证摘要不一致（存储被改动）：seq={row['seq']}")
        if payload["prev_receipt_hash"] != previous:
            raise IntegrityError(f"凭证链断裂：seq={row['seq']}")
        previous = payload["receipt_hash"]
    return dict(verified=True, count=len(rows), head=previous)


class ReceiptLedger:
    """把凭证写进与版本状态同一个 SQLite 事务，保证发布与凭证同生共死。"""

    def __init__(self, store):
        self.store = store

    def ensure_schema(self, conn):
        for statement in SCHEMA.strip().split(";"):
            if statement.strip():
                conn.execute(statement)

    def head(self, conn) -> dict | None:
        self.ensure_schema(conn)
        row = conn.execute("SELECT payload FROM receipts ORDER BY seq DESC LIMIT 1").fetchone()
        return json.loads(row["payload"]) if row else None

    def all(self, conn) -> list[dict]:
        self.ensure_schema(conn)
        return [json.loads(r["payload"]) for r in
                conn.execute("SELECT payload FROM receipts ORDER BY seq")]

    def find(self, conn, candidate_id: str, decision: str) -> dict | None:
        """按候选+决策取既有凭证；重复 accept 用它返回原凭证而不是重写。"""
        self.ensure_schema(conn)
        row = conn.execute("SELECT payload FROM receipts WHERE candidate_id=? AND decision=?",
                           (candidate_id, decision)).fetchone()
        return json.loads(row["payload"]) if row else None

    def append(self, conn, *, decision: str, candidate_id: str, parent_version: str,
               candidate_version: str, profile: str, gate_spec_hash: str, diff_hash: str,
               metrics_hash: str, evidence_hash: str, protocol_hash: str = "",
               expected_prev: str | None = None) -> dict:
        self.ensure_schema(conn)
        head = self.head(conn)
        previous = head["receipt_hash"] if head else GENESIS
        if expected_prev is not None and expected_prev != previous:
            raise StateConflict("凭证链头已变化，拒绝断链写入")
        exists = conn.execute("SELECT 1 FROM receipts WHERE candidate_id=? AND decision=?",
                              (candidate_id, decision)).fetchone()
        if exists:
            raise StateConflict("同一候选的同类决策凭证已存在")
        receipt = make_receipt(decision=decision, candidate_id=candidate_id,
                               parent_version=parent_version, candidate_version=candidate_version,
                               profile=profile, gate_spec_hash=gate_spec_hash,
                               protocol_hash=protocol_hash,
                               diff_hash=diff_hash, metrics_hash=metrics_hash,
                               evidence_hash=evidence_hash,
                               prev_receipt_hash=previous)
        conn.execute("INSERT INTO receipts(receipt_id,candidate_id,decision,payload,digest) "
                     "VALUES(?,?,?,?,?)",
                     (receipt["receipt_id"], candidate_id, decision,
                      canonical(receipt).decode("utf-8"), receipt["receipt_hash"]))
        return receipt

    def record(self, conn, decision: str, candidate: dict, report: dict) -> dict:
        """从候选与评价报告推导四个证据指纹，再写入凭证。

        ``protocol_hash`` 优先取评价报告（evolve 路径），否则取候选登记的协议；
        两者都没有时留空字符串——凭证字段仍固定，含义是“该次决策未绑定协议”。
        """
        spec = GateSpec.from_dict(candidate["validation_config"])
        from .gates import gate_profile
        evidence = dict(run_ids=list(report.get("run_ids") or []),
                        condition_hash=report.get("condition_hash"),
                        task_ids=list(report.get("task_ids") or []),
                        version_hashes=list(report.get("version_hashes") or []),
                        reasons=list(report.get("reasons") or []))
        protocol_hash = report.get("protocol_hash") or candidate.get("protocol_hash") or ""
        return self.append(
            conn, decision=decision,
            candidate_id=candidate["candidate_id"],
            parent_version=candidate["parent_version_id"],
            candidate_version=candidate["version_id"],
            profile=gate_profile(spec),
            gate_spec_hash=content_hash(spec.to_dict()),
            protocol_hash=protocol_hash,
            diff_hash=content_hash(candidate["change"]["diff"]),
            metrics_hash=content_hash(report.get("metrics") or {}),
            evidence_hash=content_hash(evidence),
        )
