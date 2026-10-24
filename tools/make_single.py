#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 terminal_logo 包合并成一个可以直接下载运行的单文件脚本。

    python3 tools/make_single.py            重新生成 standalone/logo3d.py
    python3 tools/make_single.py --check    检查 standalone/logo3d.py 是否与源码一致
"""

import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "terminal_logo")
OUT = os.path.join(ROOT, "standalone", "logo3d.py")
MODULES = ["model", "render", "term", "logos", "app"]  # 按依赖顺序

HEADER = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""终端 3D Logo（单文件版）

在终端里渲染可以用鼠标交互的 3D 像素风 Logo。只用 Python 标准库，无需安装依赖。

用法：
    python3 logo3d.py            先打开 Logo 列表
    python3 logo3d.py claude     直接显示某个 Logo（名称、别名、序号均可）
    python3 logo3d.py --help     查看全部参数

本文件由 tools/make_single.py 从 terminal_logo 包自动生成，请勿手动修改。
完整项目：https://github.com/KuRRe8/Terminal-3D-Logo
"""

__version__ = "%s"
'''


def read(name):
    with io.open(os.path.join(SRC, name), encoding="utf-8") as f:
        return f.read()


def build():
    version = re.search(r'__version__ = "([^"]+)"', read("__init__.py")).group(1)
    imports = []
    bodies = []
    for mod in MODULES:
        lines = []
        for line in read(mod + ".py").split("\n"):
            if re.match(r"\s*from \.(\w+)? ?import ", line):  # 包内相对导入
                continue
            if line.startswith("# -*- coding"):
                continue
            if re.match(r"import \w+$", line):  # 标准库导入统一放到文件开头
                if line not in imports:
                    imports.append(line)
                continue
            lines.append(line)
        body = "\n".join(lines).strip("\n")
        bar = "# " + "=" * 70
        bodies.append("\n%s\n# %s.py\n%s\n%s\n" % (bar, mod, bar, body))
    parts = [HEADER % version, "\n".join(sorted(imports)) + "\n"] + bodies
    parts.append('\n\nif __name__ == "__main__":\n    sys.exit(main())\n')
    return "\n".join(parts)


def main(argv):
    text = build()
    if "--check" in argv:
        try:
            with io.open(OUT, encoding="utf-8") as f:
                same = f.read() == text
        except IOError:
            same = False
        if not same:
            sys.stderr.write("standalone/logo3d.py 已过期，请运行 python3 tools/make_single.py\n")
            return 1
        print("standalone/logo3d.py 与源码一致")
        return 0
    with io.open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    try:
        os.chmod(OUT, 0o755)
    except OSError:
        pass
    print("已生成 %s（%d 行）" % (os.path.relpath(OUT, ROOT), text.count("\n")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
