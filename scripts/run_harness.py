"""公共底座状态检查和显式回滚入口；真实任务/模型策略尚未接入。"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import EvolutionFoundation, FoundationStore


def main():
    parser = argparse.ArgumentParser(description="离线Harness公共状态管理，不调用模型")
    parser.add_argument("command", choices=["inspect", "rollback"])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--version")
    parser.add_argument("--reason")
    parser.add_argument("--allow-simulated", action="store_true")
    args = parser.parse_args()
    root = Path(args.state_dir)
    if not (root/"foundation.sqlite3").is_file():
        parser.error("状态目录不存在；本入口不隐式初始化版本")
    store = FoundationStore(root)
    if args.command == "rollback":
        if not args.version or not args.reason:
            parser.error("回滚必须明确--version与--reason")
        store.rollback(args.version, args.reason, allow_simulated=args.allow_simulated)
    print(json.dumps(EvolutionFoundation(store).resume(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
