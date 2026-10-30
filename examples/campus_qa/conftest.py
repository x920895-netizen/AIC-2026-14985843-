"""让 `pytest examples/campus_qa/tests` 能从仓库根目录直接运行。"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for path in (REPO, HERE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
