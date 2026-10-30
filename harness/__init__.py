"""Harness公共底座；本包默认不创建模型客户端、不发送网络请求。

对外能力：
- 版本与预算治理：``EvolutionFoundation`` / ``FoundationStore``
- 两级门禁：``GateSpec.screen`` / ``GateSpec.release`` / ``evaluate_gate`` / ``assess_gate``
- 统计口径：``wilson_interval`` / ``mcnemar_exact`` / ``bca_bootstrap`` / ``mde_proportions``
- 选择偏差：``build_summary`` / ``SelectionTracker``
- 决策凭证：``verify_chain`` / ``ReceiptLedger``
- 执行器：``OfflineExecutor``（无发送能力）/ ``OnlineExecutor``（接 runner、限流）
- 协议：``ProtocolSpec``（划分/评价器/阈值/重复/种子）/ ``TestGuard``
- 可观测：``TraceLog``（JSONL 事件）
- 在线闭环：``EvolutionFoundation.evolve`` / ``resume(run_id)``
"""

__version__ = "1.2.0"

from .contracts import CandidateProposal, EvidenceRef, GateSpec, RuntimeTask, TargetKind
from .controller import EvolutionFoundation
from .execution import Executor, OfflineExecutor, OnlineExecutor
from .gates import RELEASE, SCREEN, assess_gate, evaluate_gate, gate_profile
from .protocol import ProtocolSpec, TestSetGuard, build_protocol, protocol_warnings
from .receipt import ReceiptLedger, verify_chain
from .selection import SelectionTracker, build_summary
from .stats import (analyze_paired, bca_bootstrap, mcnemar_exact, mde_proportions,
                    percentile_interval, required_pairs, wilson_interval)
from .storage import FoundationStore, IntegrityError, StateConflict
from .trace import TraceLog

__all__ = ["__version__", "CandidateProposal", "EvidenceRef", "GateSpec", "RuntimeTask", "TargetKind",
           "EvolutionFoundation", "FoundationStore", "IntegrityError", "StateConflict",
           "SCREEN", "RELEASE", "evaluate_gate", "assess_gate", "gate_profile",
           "ReceiptLedger", "verify_chain", "Executor", "OfflineExecutor", "OnlineExecutor",
           "ProtocolSpec", "TestSetGuard", "build_protocol", "protocol_warnings",
           "SelectionTracker", "build_summary", "TraceLog",
           "wilson_interval", "mcnemar_exact", "bca_bootstrap", "mde_proportions",
           "percentile_interval", "required_pairs", "analyze_paired"]
