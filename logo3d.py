#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""终端 3D Logo 启动脚本：python3 logo3d.py [logo]"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from terminal_logo.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
