#!/usr/bin/env python3
"""mediaharvest 命令行入口 —— 项目根目录入口。

在 PyCharm 里调试命令行抓取时，推荐用这个文件：

  1. 右键本文件 → Modify Run Configuration
  2. 在 "Parameters" 里填入目标网址，例如：
         https://example.com/gallery --list
  3. 点 Run / Debug 即可

等价于命令行执行 ``./mh <参数>``。
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from mediaharvest.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
