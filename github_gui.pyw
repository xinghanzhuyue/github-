#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双击即用的入口：.pyw 由系统关联到 pythonw.exe，启动界面全程不出现终端窗口。

命令行里跑 py -3 github_gui.pyw 效果相同（py 启动器对 .pyw 也会用 pythonw），
参数原样传给 github_gui.py，例如：py -3 github_gui.pyw --flat
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import github_gui

if __name__ == "__main__":
    sys.exit(github_gui.main())
