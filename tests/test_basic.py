# -*- coding: utf-8 -*-
"""基础测试：python3 -m unittest discover -s tests"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from terminal_logo.app import (UsageError, parse_args, q_axis, q_mat, q_mul, spin_ease)  # noqa: E402
from terminal_logo.logos import LOGOS, find_logo, search, suggest  # noqa: E402
from terminal_logo.model import Model  # noqa: E402
from terminal_logo.render import DEFAULT_BG, Renderer, mat_mul, rot_x, rot_y  # noqa: E402
from terminal_logo.term import InputParser, Key, Mouse, Screen, text_width  # noqa: E402


class LogoTests(unittest.TestCase):
    def test_required_logos_present(self):
        keys = [l.key for l in LOGOS]
        expected = ["claude", "codex", "github", "gemini", "doubao", "deepseek", "baidu", "vscode", "apple",
                    "netease", "wechat", "intel", "amd", "nvidia", "c", "cpp", "python", "js", "ubuntu",
                    "linux", "redhat", "windows", "vim", "qt", "matlab", "latex"]
        self.assertEqual(keys, expected)

    def test_lookup(self):
        self.assertEqual(find_logo("C++").key, "cpp")
        self.assertEqual(find_logo("豆包").key, "doubao")
        self.assertEqual(find_logo("VS Code").key, "vscode")
        self.assertEqual(find_logo("螃蟹").key, "claude")
        self.assertEqual(find_logo("6").key, "deepseek")
        self.assertIsNone(find_logo("nope"))
        self.assertIn("python", suggest("pyhton"))
        self.assertEqual([l.key for l in search("py")], ["python"])

    def test_models_build_and_render(self):
        r = Renderer()
        r.configure(80, 48, "dark")
        rot = mat_mul(rot_x(0.3), rot_y(-0.5))
        for logo in LOGOS:
            m = Model(logo)
            self.assertGreater(len(m.faces), 0, logo.key)
            buf = r.render(m, rot, 40, 24, min(60.0 / m.W, 36.0 / m.H))
            changed = sum(1 for a, b in zip(buf, r.bg) if a != b)
            self.assertGreater(changed, 200, logo.key)

    def test_back_side(self):
        # 正面看不到背面；转 180° 后看到的仍是完整的 logo
        m = Model(find_logo("github"))
        r = Renderer()
        r.configure(60, 60, "black")
        s = 50.0 / m.W
        front = r.render(m, rot_y(0.0), 30, 30, s)
        back = r.render(m, rot_y(math.pi), 30, 30, s)
        n_front = sum(1 for c in front if c)
        n_back = sum(1 for c in back if c)
        self.assertAlmostEqual(n_front, n_back, delta=n_front * 0.05)

    def test_term_background(self):
        r = Renderer()
        r.configure(10, 10, "term")
        self.assertTrue(all(c == DEFAULT_BG for c in r.bg))


class MathTests(unittest.TestCase):
    def test_spin_ease(self):
        self.assertEqual(spin_ease(0.0), 0.0)
        self.assertEqual(spin_ease(1.0), 1.0)
        vals = [spin_ease(i / 100.0) for i in range(101)]
        self.assertTrue(all(b >= a for a, b in zip(vals, vals[1:])))
        # 加速快：前 20% 时间里已经转过不少；减速慢：最后 25% 时间只转一点点
        self.assertGreater(vals[20], 0.2)
        self.assertLess(1.0 - vals[75], 0.05)

    def test_quaternion_full_turn_is_identity(self):
        q = q_axis(0, 1, 0, 2 * math.pi)
        m = q_mat(q_mul(q, (1.0, 0.0, 0.0, 0.0)))
        for a, b in zip(m, (1, 0, 0, 0, 1, 0, 0, 0, 1)):
            self.assertAlmostEqual(a, b, places=9)


class CliTests(unittest.TestCase):
    def test_parse(self):
        o = parse_args(["claude", "-d", "5", "--bg=light", "--no-grid", "-p", "--size", "40x12", "--yaw", "-30"])
        self.assertEqual(o.logo, "claude")
        self.assertEqual(o.depth, 5)
        self.assertEqual(o.bg, "light")
        self.assertFalse(o.grid)
        self.assertTrue(o.print)
        self.assertEqual(o.size, (40, 12))
        self.assertEqual(o.yaw, -30.0)

    def test_errors(self):
        for bad in (["--bogus"], ["-d", "x"], ["-d", "99"], ["--bg", "pink"], ["a", "b"], ["--size", "3"]):
            with self.assertRaises(UsageError):
                parse_args(bad)


class TermTests(unittest.TestCase):
    def test_parser(self):
        p = InputParser()
        evs = p.feed("\x1b[A\x1b[<0;10;5M\x1b[<32;11;5M\x1b[<0;11;5m\x1b[<64;1;1Mq")
        self.assertIsInstance(evs[0], Key)
        self.assertEqual(evs[0].name, "up")
        self.assertEqual([(e.kind, e.button, e.x, e.y) for e in evs[1:5]],
                         [("press", 0, 9, 4), ("drag", 0, 10, 4), ("release", 0, 10, 4), ("wheel", 1, 0, 0)])
        self.assertIsInstance(evs[4], Mouse)
        self.assertEqual(evs[5].name, "q")
        self.assertEqual(p.feed("\x1b"), [])
        self.assertEqual(p.feed("", final=True)[0].name, "esc")

    def test_screen_diff(self):
        sc = Screen()
        row = [(0xFF0000 << 25) | 0x0000FF, ("中", 0xFFFFFF, 0), None, ("a", 0xFFFFFF, 0)]
        first = sc.diff([row])
        self.assertIn("▀", first)
        self.assertIn("中", first)
        self.assertEqual(sc.diff([list(row)]), "")
        row2 = list(row)
        row2[3] = ("b", 0xFFFFFF, 0)
        out = sc.diff([row2])
        self.assertIn("b", out)
        self.assertNotIn("中", out)

    def test_text_width(self):
        self.assertEqual(text_width("Logo 列表"), 9)


if __name__ == "__main__":
    unittest.main()
