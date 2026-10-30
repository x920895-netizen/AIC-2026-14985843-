"""本地原始账本的前置检查。

逐题轨迹、逐请求账本与划分文件含第三方语料片段和调用明细，不随开源包分发。
缺少这些文件时，报告脚本应给出可读提示并退出，而不是抛 ``FileNotFoundError``。
"""

import sys
from pathlib import Path

HINT = ("这些是本地原始账本/划分文件，开源包只包含脱敏汇总。"
        "请先在本地实验目录完成对应轮次后再运行本脚本。"
        "开源包一键可复现的入口是：pytest tests/、scripts/demo_submission.py、examples/gate_replay.py。")


def require_files(*paths: str | Path) -> None:
    missing = [Path(p) for p in paths if not Path(p).is_file()]
    if not missing:
        return
    print("无法生成报告：缺少本地原始账本", file=sys.stderr)
    for path in missing:
        print(f"  - {path}", file=sys.stderr)
    print(HINT, file=sys.stderr)
    raise SystemExit(2)
