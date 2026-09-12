# -*- coding: utf-8 -*-
"""开发与运维脚本（不属于运行时）。

用 `python -m tools.<脚本名>` 运行，不要 `python tools/<脚本名>.py`——
后者会把 sys.path 指向 tools/，import weizhi 就找不到了。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
