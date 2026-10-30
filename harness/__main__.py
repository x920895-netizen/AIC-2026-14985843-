"""``python -m harness`` 等价于 ``python -m harness.cli``。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
