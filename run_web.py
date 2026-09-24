#!/usr/bin/env python3
"""mediaharvest Web 界面 —— 项目根目录入口。

在 PyCharm 里直接右键本文件 → Run 即可启动 Web 界面。
等价于命令行执行 ``./mh-web``。

默认地址 http://127.0.0.1:8848
"""
from __future__ import annotations

import os
import sys

# 确保项目根目录在 sys.path 中（PyCharm 直接运行时通常已包含，
# 但用其它方式执行时需要显式添加）
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from mediaharvest.web import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
