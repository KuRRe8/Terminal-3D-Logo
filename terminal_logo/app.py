# -*- coding: utf-8 -*-
"""交互式终端 3D Logo 查看器：状态、动画、交互与界面。"""

import math
import os
import signal
import sys
import time

from . import __version__
from .logos import LOGOS, find_logo, search, suggest
from .model import Model
from .render import DEFAULT_BG, THEMES, THEME_NAMES, Renderer, mat_mul, rot_x, rot_y
from .term import IS_WINDOWS, Key, Mouse, Screen, Terminal, char_width, clip_text, encode_cells, text_width

TAU = 2.0 * math.pi
SPIN_TIME = 1.7          # 单击旋转一圈的时长（秒）
INERTIA_TAU = 0.35       # 拖动松手后惯性衰减的时间常数
ZOOM_MIN, ZOOM_MAX = 0.25, 12.0


# ====================================================================== 数学
def q_axis(x, y, z, angle):
    s = math.sin(angle / 2.0)
    return (math.cos(angle / 2.0), x * s, y * s, z * s)


def q_mul(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (aw * bw - ax * bx - ay * by - az * bz,
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw)


def q_norm(q):
    n = math.sqrt(sum(c * c for c in q)) or 1.0
    return tuple(c / n for c in q)


def q_mat(q):
    w, x, y, z = q
    return (1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y),
            2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x),
            2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y))


def q_slerp(a, b, t):
    d = sum(i * j for i, j in zip(a, b))
    if d < 0:
        b = tuple(-c for c in b)
        d = -d
    if d > 0.9995:
        return q_norm(tuple(i + (j - i) * t for i, j in zip(a, b)))
    th = math.acos(d)
    s = math.sin(th)
    wa = math.sin((1 - t) * th) / s
    wb = math.sin(t * th) / s
    return tuple(wa * i + wb * j for i, j in zip(a, b))


Q_ID = (1.0, 0.0, 0.0, 0.0)


def spin_ease(s, a=0.14):
    """单击旋转的缓动：前 a 段快速加速，之后用三次曲线缓慢减速（速度连续）。"""
    if s <= 0:
        return 0.0
    if s >= 1:
        return 1.0
    area = a / 2.0 + (1.0 - a) / 3.0
    if s < a:
        return (s * s / (2.0 * a)) / area
    r = (1.0 - s) / (1.0 - a)
    return (a / 2.0 + (1.0 - a) / 3.0 * (1.0 - r * r * r)) / area


def ease_out_cubic(s):
    s = min(1.0, max(0.0, s))
    return 1.0 - (1.0 - s) ** 3


class Anim(object):
    """绕摄像机空间某个轴的一段旋转动画，结束后并入当前姿态。"""

    __slots__ = ("start", "dur", "axis", "angle", "ease")

    def __init__(self, start, dur, axis, angle, ease):
        self.start, self.dur, self.axis, self.angle, self.ease = start, dur, axis, angle, ease

    def value(self, now):
        return self.angle * self.ease((now - self.start) / self.dur)

    def done(self, now):
        return now - self.start >= self.dur


# ====================================================================== 配色
UI_DARK = dict(fg=0xE6E8EF, dim=0x8B92A5, accent=0x8AB4FF, panel=0x15171D, panel2=0x20232C,
               sel=0x2F4F8F, selfg=0xFFFFFF, toast=0x2A2E39, warn=0xFFB86C)
UI_LIGHT = dict(fg=0x1F2328, dim=0x6B7280, accent=0x2F5BD3, panel=0xFFFFFF, panel2=0xE9ECF2,
                sel=0xC9D8FF, selfg=0x0B1530, toast=0xFFFFFF, warn=0xB45309)

HINTS = [("左键单击", "旋转一圈"), ("左键拖动", "3D 检视"), ("右键拖动", "平移"), ("滚轮", "缩放"),
         ("Tab", "列表"), ("H", "帮助"), ("Q", "退出")]

HELP_LINES = [
    ("title", "操作说明"),
    ("head", "鼠标"),
    ("左键单击", "水平旋转 360°（快速加速，缓慢减速）"),
    ("左键拖动", "3D 检视，自由旋转（松手有惯性）"),
    ("右键拖动", "平移（中键或 Ctrl / Alt + 左键拖动亦可）"),
    ("滚轮", "以鼠标位置为中心缩放"),
    ("head", "键盘"),
    ("方向键", "旋转 15°"),
    ("Shift + 方向键", "平移"),
    ("+  -", "放大 / 缩小"),
    ("空格 / 回车", "水平旋转一圈"),
    ("R", "重置视角"),
    ("A", "自动旋转 开 / 关"),
    ("G", "像素网格线 开 / 关"),
    ("B", "切换背景"),
    ("[  ]", "减小 / 增加厚度"),
    ("N  P", "下一个 / 上一个 Logo"),
    ("Tab  L", "打开 Logo 列表"),
    ("F", "显示帧率"),
    ("H  ?", "显示 / 隐藏帮助"),
    ("Q  Esc", "退出"),
    ("foot", "按任意键关闭"),
]


def main_color(logo):
    counts = {}
    for r in logo.rows:
        for ch in r:
            if ch != ".":
                counts[ch] = counts.get(ch, 0) + 1
    ch = max(counts, key=counts.get)
    h = logo.palette[ch][0].lstrip("#")
    return int(h, 16)


# ====================================================================== 应用
class App(object):
    def __init__(self, term, opts, logo=None):
        self.term = term
        self.screen = Screen()
        self.renderer = Renderer()
        self.opts = opts
        self.theme = opts.bg
        self.grid = opts.grid
        self.auto = opts.auto
        self.show_fps = False
        self.depth = opts.depth
        self.fps_limit = max(5, min(240, opts.fps))
        self.models = {}
        self.chips = dict((l.key, main_color(l)) for l in LOGOS)
        self.running = True
        self.dirty = True
        self.cols, self.rows = term.size()

        self.cur = LOGOS.index(logo) if logo is not None else 0
        self.reset_view()

        # 列表状态
        self.mode = "view"
        self.help = False
        self.query = ""
        self.filtered = list(LOGOS)
        self.sel = 0
        self.scroll = 0
        self.list_prev = None
        self.list_from_start = False

        self.toast_text = None
        self.toast_until = 0.0
        self.drag = None
        self.last_update = time.monotonic()
        self.frame_times = []
        self.list_t0 = time.monotonic()

        now = time.monotonic()
        if logo is None:
            self.open_list(start=True)
        else:
            if opts.intro:
                self.spin(now)
            self.toast("按 H 查看操作说明", now, 3.0)

    # ------------------------------------------------------------------ 状态
    @property
    def logo(self):
        return LOGOS[self.cur]

    def model(self):
        key = (self.logo.key, self.depth)
        m = self.models.get(key)
        if m is None:
            m = Model(self.logo, self.depth)
            self.models[key] = m
        return m

    def reset_view(self):
        self.q = Q_ID
        self.pan = [0.0, 0.0]
        self.zoom = 1.0
        self.anims = []
        self.omega = [0.0, 0.0]
        self.reset_anim = None

    def set_logo(self, idx, spin=False, now=None):
        self.cur = idx % len(LOGOS)
        self.reset_view()
        if spin:
            self.spin(now or time.monotonic())
        self.dirty = True

    def spin(self, now):
        self.anims.append(Anim(now, SPIN_TIME, (0.0, 1.0, 0.0), TAU, spin_ease))
        self.omega = [0.0, 0.0]
        self.dirty = True

    def toast(self, text, now=None, dur=1.6):
        self.toast_text = text
        self.toast_until = (now or time.monotonic()) + dur
        self.dirty = True

    def animating(self, now):
        return bool(self.anims or self.reset_anim or self.auto or self.omega[0] or self.omega[1]
                    or self.mode == "list")

    def frame_interval(self):
        busy = self.anims or self.reset_anim or self.auto or self.omega[0] or self.omega[1] or self.drag
        if self.mode == "list" and not busy:
            return 1.0 / min(self.fps_limit, 30)  # 列表里的预览轻微摆动，30 帧足够
        return 1.0 / self.fps_limit

    def idle_timeout(self, now):
        t = 0.25
        if self.toast_text:
            t = min(t, max(0.0, self.toast_until - now) + 0.01)
        return t

    # ------------------------------------------------------------------ 布局
    def panel_width(self):
        if self.mode != "list":
            return 0
        if self.cols < 56:
            return self.cols
        return max(26, min(38, self.cols // 3))

    def view_center(self):
        pw = self.panel_width()
        avail = self.cols - pw
        if avail <= 4:
            avail, pw = self.cols, 0
        return pw + avail / 2.0, self.rows, avail

    def base_scale(self, m):
        _, _, avail_w = self.view_center()
        avail_h = max(4, self.rows * 2 - 4)
        return max(0.3, min(0.82 * avail_w / m.W, 0.78 * avail_h / m.H))

    def drag_gain(self):
        pw, ph = self.cols, self.rows * 2
        return math.pi / max(48.0, 0.9 * min(pw, ph))

    # ------------------------------------------------------------------ 动画
    def update(self, now):
        dt = min(0.1, max(0.0, now - self.last_update))
        self.last_update = now
        if self.auto and not self.drag:
            self.q = q_norm(q_mul(q_axis(0, 1, 0, 0.9 * dt), self.q))
        if (self.omega[0] or self.omega[1]) and not self.drag:
            wx, wy = self.omega
            self.q = q_norm(q_mul(q_axis(0, 1, 0, wy * dt), q_mul(q_axis(1, 0, 0, wx * dt), self.q)))
            k = math.exp(-dt / INERTIA_TAU)
            self.omega = [wx * k, wy * k]
            if abs(self.omega[0]) + abs(self.omega[1]) < 0.05:
                self.omega = [0.0, 0.0]
        keep = []
        for a in self.anims:
            if a.done(now):
                self.q = q_norm(q_mul(q_axis(a.axis[0], a.axis[1], a.axis[2], a.angle), self.q))
            else:
                keep.append(a)
        self.anims = keep
        if self.reset_anim:
            t0, q0, pan0, zoom0 = self.reset_anim
            s = ease_out_cubic((now - t0) / 0.5)
            self.q = q_slerp(q0, Q_ID, s)
            self.pan = [pan0[0] * (1 - s), pan0[1] * (1 - s)]
            self.zoom = zoom0 + (1.0 - zoom0) * s
            if s >= 1.0:
                self.reset_anim = None
        if self.toast_text and now > self.toast_until:
            self.toast_text = None
            self.dirty = True

    def rotation(self, now):
        q = self.q
        for a in self.anims:
            q = q_mul(q_axis(a.axis[0], a.axis[1], a.axis[2], a.value(now)), q)
        rot = q_mat(q_norm(q))
        if self.mode == "list":
            t = now - self.list_t0
            rot = mat_mul(rot_y(0.38 * math.sin(t * 1.1)), mat_mul(rot_x(0.10 * math.sin(t * 0.7)), rot))
        return rot

    # ------------------------------------------------------------------ 绘制
    def ui(self):
        return UI_LIGHT if self.theme == "light" else UI_DARK

    def draw(self, now):
        cols, rows = self.cols, self.rows
        pw, ph = cols, rows * 2
        self.renderer.configure(pw, ph, self.theme)
        m = self.model()
        cx, cy, _ = self.view_center()
        scale = self.base_scale(m) * self.zoom
        t0 = time.perf_counter()
        cb = self.renderer.render(m, self.rotation(now), cx + self.pan[0], cy + self.pan[1], scale, self.grid)
        t_render = time.perf_counter() - t0

        self.ov = {}
        self.draw_bars()
        if self.mode == "list":
            self.draw_list()
        if self.toast_text:
            self.draw_toast()
        if self.help:
            self.draw_help()

        out_rows = []
        for r in range(rows):
            a = 2 * r * pw
            top = cb[a:a + pw]
            bot = cb[a + pw:a + 2 * pw]
            row = [(t << 25) | b for t, b in zip(top, bot)]
            ov = self.ov.get(r)
            if ov:
                for c, cell in ov.items():
                    row[c] = cell
            out_rows.append(row)
        s = self.screen.diff(out_rows)
        if s:
            self.term.write("\x1b[?2026h" + s + "\x1b[?2026l")
            self.term.flush()
        self.frame_times.append(now)
        if len(self.frame_times) > 30:
            self.frame_times.pop(0)
        self.last_render_ms = t_render * 1000

    # --- 文本工具 ----------------------------------------------------------
    def bg_at(self, row, col):
        bg = self.renderer.bg
        i = (2 * row + 1) * self.cols + col
        return bg[i] if 0 <= i < len(bg) else DEFAULT_BG

    def put(self, row, col, text, fg, bg=None):
        """在 (row, col) 写文字，bg=None 表示透明（使用背景色）。返回结束列。"""
        if row < 0 or row >= self.rows:
            return col
        line = self.ov.setdefault(row, {})
        for ch in text:
            w = char_width(ch)
            if w == 0:
                continue
            if col < 0:
                col += w
                continue
            if col + w > self.cols:
                break
            # 避免把已有的宽字符劈成两半
            if line.get(col, 0) is None and col > 0 and line.get(col - 1):
                prev = line[col - 1]
                line[col - 1] = (" ", prev[1], prev[2])
            old = line.get(col)
            if old and char_width(old[0]) == 2 and w == 1 and col + 1 < self.cols:
                line[col + 1] = (" ", old[1], old[2])
            b = self.bg_at(row, col) if bg is None else bg
            line[col] = (ch, fg, b)
            if w == 2:
                nxt = line.get(col + 2, 0)
                line[col + 1] = None
                if nxt is None:
                    line[col + 2] = (" ", fg, b)
            col += w
        return col

    def fill(self, row, c0, c1, bg):
        if 0 <= row < self.rows:
            self.put(row, c0, " " * max(0, min(c1, self.cols) - c0), bg, bg)

    # --- 标题栏与提示栏 ------------------------------------------------------
    def draw_bars(self):
        ui = self.ui()
        cols, rows = self.cols, self.rows
        if rows < 3 or cols < 10:
            return
        left = self.panel_width() if self.mode == "list" and self.panel_width() < cols else 0
        if left >= cols:
            return
        title_l = "终端 3D Logo"
        name = self.logo.name
        right = "%d/%d" % (self.cur + 1, len(LOGOS))
        if self.show_fps and len(self.frame_times) > 1:
            span = self.frame_times[-1] - self.frame_times[0]
            fps = (len(self.frame_times) - 1) / span if span > 0 else 0
            right = "%.0f fps  %.1f ms  %s" % (fps, getattr(self, "last_render_ms", 0), right)
        c = left + 1
        if text_width(title_l) + text_width(name) + text_width(right) + 8 <= cols - left:
            c = self.put(0, c, title_l, ui["dim"])
            c = self.put(0, c, "  ·  ", ui["dim"])
        self.put(0, c, clip_text(name, max(0, cols - c - text_width(right) - 2)), ui["fg"])
        self.put(0, cols - text_width(right) - 1, right, ui["dim"])
        if self.mode == "list":
            return
        # 底部提示：尽量多放几项，放不下就省略后面的
        items = list(HINTS)
        while items:
            width = sum(text_width(k) + 1 + text_width(v) for k, v in items) + 3 * (len(items) - 1)
            if width <= cols - 2:
                break
            items.pop(-3 if len(items) > 3 else -1)
        if not items:
            return
        c = max(1, (cols - width) // 2)
        for i, (k, v) in enumerate(items):
            if i:
                c = self.put(rows - 1, c, "   ", ui["dim"])
            c = self.put(rows - 1, c, k, ui["accent"])
            c = self.put(rows - 1, c, " " + v, ui["dim"])

    def draw_toast(self):
        ui = self.ui()
        text = " " + self.toast_text + " "
        w = text_width(text)
        pw = self.panel_width() if self.mode == "list" else 0
        avail = self.cols - pw if pw < self.cols else self.cols
        c = (pw if pw < self.cols else 0) + max(0, (avail - w) // 2)
        r = self.rows - 3 if self.mode == "view" else self.rows - 2
        if r < 1:
            r = self.rows - 1
        self.put(r, c, text, ui["fg"], ui["toast"])

    # --- 列表 ----------------------------------------------------------------
    def list_geom(self):
        top = 3
        bottom = self.rows - 2  # 不含
        return top, max(top, bottom)

    def draw_list(self):
        ui = self.ui()
        w = self.panel_width()
        rows = self.rows
        for r in range(rows):
            self.fill(r, 0, w, ui["panel"])
        head = " 选择 Logo"
        self.put(0, 0, head, ui["accent"], ui["panel"])
        cnt = "%d 个 " % len(self.filtered) if self.query else "共 %d 个 " % len(LOGOS)
        self.put(0, max(text_width(head) + 1, w - text_width(cnt)), cnt, ui["dim"], ui["panel"])
        # 搜索框
        if rows > 2:
            self.fill(1, 1, w - 1, ui["panel2"])
            if self.query:
                c = self.put(1, 2, "搜索: ", ui["dim"], ui["panel2"])
                c = self.put(1, c, clip_text(self.query, max(0, w - c - 3)), ui["fg"], ui["panel2"])
                self.put(1, c, "_", ui["accent"], ui["panel2"])
            else:
                self.put(1, 2, clip_text("输入文字搜索...", w - 4), ui["dim"], ui["panel2"])
        top, bottom = self.list_geom()
        vis = bottom - top
        if vis <= 0:
            return
        self.fix_scroll()
        if not self.filtered:
            self.put(top, 2, clip_text("没有匹配的 Logo", w - 3), ui["warn"], ui["panel"])
        for k in range(vis):
            i = self.scroll + k
            if i >= len(self.filtered):
                break
            logo = self.filtered[i]
            r = top + k
            selected = i == self.sel
            bg = ui["sel"] if selected else ui["panel"]
            fg = ui["selfg"] if selected else ui["fg"]
            self.fill(r, 0, w, bg)
            self.put(r, 0, ">" if selected else " ", ui["accent"] if not selected else fg, bg)
            self.put(r, 2, "  ", self.chips[logo.key], self.chips[logo.key])
            num = "%2d " % (LOGOS.index(logo) + 1)
            c = self.put(r, 5, num, ui["dim"] if not selected else fg, bg)
            self.put(r, c, clip_text(logo.name, max(0, w - c - 1)), fg, bg)
        # 滚动指示
        if self.scroll > 0:
            self.put(top - 1, w - 4, "^^", ui["dim"], ui["panel"])
        if self.scroll + vis < len(self.filtered):
            self.put(bottom, w - 4, "vv", ui["dim"], ui["panel"])
        foot = " 上下键选择  回车确认  Esc 返回"
        self.put(rows - 1, 0, clip_text(foot, w), ui["dim"], ui["panel"])

    def fix_scroll(self):
        top, bottom = self.list_geom()
        vis = max(1, bottom - top)
        if self.sel < self.scroll:
            self.scroll = self.sel
        if self.sel >= self.scroll + vis:
            self.scroll = self.sel - vis + 1
        self.scroll = max(0, min(self.scroll, max(0, len(self.filtered) - vis)))

    def open_list(self, start=False):
        self.mode = "list"
        self.help = False
        self.list_from_start = start
        self.list_prev = None if start else self.cur
        self.query = ""
        self.filtered = list(LOGOS)
        self.sel = self.cur
        self.scroll = 0
        self.list_t0 = time.monotonic()
        self.drag = None
        self.reset_view()
        self.screen.invalidate()
        self.dirty = True

    def close_list(self, confirm, now):
        if confirm and self.filtered:
            self.set_logo(LOGOS.index(self.filtered[self.sel]), spin=True, now=now)
        elif self.list_prev is not None:
            self.set_logo(self.list_prev)
        elif self.list_from_start:
            self.running = False
            return
        self.mode = "view"
        self.screen.invalidate()
        self.dirty = True

    def list_select(self, i):
        if not self.filtered:
            return
        i = max(0, min(len(self.filtered) - 1, i))
        if i != self.sel or LOGOS.index(self.filtered[i]) != self.cur:
            self.sel = i
            self.set_logo(LOGOS.index(self.filtered[i]))
        self.fix_scroll()
        self.dirty = True

    def refilter(self):
        self.filtered = search(self.query)
        self.sel = 0
        self.scroll = 0
        if self.filtered:
            self.set_logo(LOGOS.index(self.filtered[0]))
        self.dirty = True

    # --- 帮助 ----------------------------------------------------------------
    def draw_help(self):
        ui = self.ui()
        kw = max(text_width(k) for k, v in HELP_LINES if k not in ("title", "head", "foot"))
        vw = max(text_width(v) for k, v in HELP_LINES if k not in ("title", "head", "foot"))
        w = min(self.cols, kw + vw + 8)
        h = min(self.rows, len(HELP_LINES) + 2)
        c0 = max(0, (self.cols - w) // 2)
        r0 = max(0, (self.rows - h) // 2)
        for r in range(r0, r0 + h):
            self.fill(r, c0, c0 + w, ui["panel"])
        lines = HELP_LINES
        if h - 2 < len(lines):  # 终端太矮：去掉分组标题
            lines = [l for l in lines if l[0] not in ("head",)][:max(0, h - 2)]
        r = r0 + 1
        for k, v in lines:
            if r >= r0 + h - 1 and k != "foot":
                break
            if k == "title":
                self.put(r, c0 + max(1, (w - text_width(v)) // 2), v, ui["accent"], ui["panel"])
            elif k == "head":
                self.put(r, c0 + 2, v, ui["warn"], ui["panel"])
            elif k == "foot":
                self.put(min(r, r0 + h - 1), c0 + max(1, (w - text_width(v)) // 2), v, ui["dim"], ui["panel"])
            else:
                self.put(r, c0 + 4, k, ui["accent"], ui["panel"])
                self.put(r, c0 + 6 + kw, clip_text(v, max(0, w - kw - 7)), ui["fg"], ui["panel"])
            r += 1

    # ------------------------------------------------------------------ 输入
    def handle(self, ev, now):
        if isinstance(ev, Key):
            self.on_key(ev, now)
        elif isinstance(ev, Mouse):
            self.on_mouse(ev, now)

    def on_key(self, k, now):
        name = k.name
        self.dirty = True
        if name == "ctrl+c":
            self.running = False
            return
        if name == "ctrl+z" and not IS_WINDOWS:
            self.suspend()
            return
        if name in ("ctrl+l",):
            self.screen.invalidate()
            return
        if self.help:
            self.help = False
            return
        if self.mode == "list":
            self.on_list_key(k, now)
            return
        low = name.lower() if k.is_char else name
        if low in ("q", "esc"):
            self.running = False
        elif low in ("h", "?"):
            self.help = True
        elif name in ("tab", "l", "L", "m", "M") or (name == "/"):
            self.open_list()
        elif low in (" ", "enter"):
            self.spin(now)
        elif low == "r" or name == "0":
            self.reset_anim = (now, self.q, list(self.pan), self.zoom)
            self.anims = []
            self.omega = [0.0, 0.0]
        elif name in ("left", "right", "up", "down") and k.shift:
            step = 4.0
            dx = {"left": -step, "right": step}.get(name, 0.0)
            dy = {"up": -step, "down": step}.get(name, 0.0)
            self.pan[0] += dx
            self.pan[1] += dy
            self.reset_anim = None
        elif name in ("left", "right", "up", "down"):
            ang = math.radians(15)
            axis = (0.0, 1.0, 0.0) if name in ("left", "right") else (1.0, 0.0, 0.0)
            sign = 1 if name in ("right", "down") else -1
            self.anims.append(Anim(now, 0.22, axis, sign * ang, ease_out_cubic))
        elif name in ("+", "="):
            self.zoom_at(1.2, None)
        elif name in ("-", "_"):
            self.zoom_at(1 / 1.2, None)
        elif low == "a":
            self.auto = not self.auto
            self.toast("自动旋转：" + ("开" if self.auto else "关"), now)
        elif low == "g":
            self.grid = not self.grid
            self.toast("像素网格线：" + ("开" if self.grid else "关"), now)
        elif low == "b":
            self.theme = THEMES[(THEMES.index(self.theme) + 1) % len(THEMES)]
            self.toast("背景：" + THEME_NAMES[self.theme], now)
        elif name in ("[", "]"):
            cur = self.depth or self.model().depth
            self.depth = max(1, min(16, cur + (1 if name == "]" else -1)))
            self.toast("厚度：%d" % self.depth, now)
        elif low == "n" or name == "pgdn":
            self.set_logo(self.cur + 1, spin=True, now=now)
        elif low == "p" or name == "pgup":
            self.set_logo(self.cur - 1, spin=True, now=now)
        elif low == "f":
            self.show_fps = not self.show_fps
            self.frame_times = []

    def on_list_key(self, k, now):
        name = k.name
        if name == "esc":
            if self.query:
                self.query = ""
                self.refilter()
                if self.list_prev is not None:
                    pass
            else:
                self.close_list(False, now)
        elif name in ("enter", "tab"):
            if self.filtered:
                self.close_list(True, now)
        elif name in ("up", "ctrl+p", "backtab"):
            self.list_select(self.sel - 1)
        elif name in ("down", "ctrl+n"):
            self.list_select(self.sel + 1)
        elif name == "pgup":
            top, bottom = self.list_geom()
            self.list_select(self.sel - max(1, bottom - top - 1))
        elif name == "pgdn":
            top, bottom = self.list_geom()
            self.list_select(self.sel + max(1, bottom - top - 1))
        elif name == "home":
            self.list_select(0)
        elif name == "end":
            self.list_select(len(self.filtered) - 1)
        elif name == "backspace":
            if self.query:
                self.query = self.query[:-1]
                self.refilter()
        elif name == "ctrl+u":
            self.query = ""
            self.refilter()
        elif k.is_char and not k.alt and not k.ctrl and name >= " ":
            self.query += name
            self.refilter()

    def zoom_at(self, factor, pos):
        cx, cy, _ = self.view_center()
        nz = max(ZOOM_MIN, min(ZOOM_MAX, self.zoom * factor))
        f = nz / self.zoom
        if pos is None:
            mx, my = cx, cy
        else:
            mx, my = pos
        self.pan[0] = (mx - cx) - f * (mx - cx - self.pan[0])
        self.pan[1] = (my - cy) - f * (my - cy - self.pan[1])
        self.zoom = nz
        self.reset_anim = None
        self.dirty = True

    def on_mouse(self, ev, now):
        self.dirty = True
        if self.help:
            if ev.kind in ("press", "wheel"):
                self.help = False
            return
        px, py = ev.x + 0.5, ev.y * 2 + 1.0
        if self.mode == "list":
            w = self.panel_width()
            if ev.x < w:
                top, bottom = self.list_geom()
                if ev.kind == "wheel":
                    self.list_select(self.sel - ev.button)
                elif ev.kind == "press" and ev.button == 0 and top <= ev.y < bottom:
                    i = self.scroll + ev.y - top
                    if i < len(self.filtered):
                        self.list_select(i)
                        self.close_list(True, now)
                return
            if ev.kind == "press" and ev.button == 0 and self.filtered:
                self.close_list(True, now)
            elif ev.kind == "wheel":
                self.list_select(self.sel - ev.button)
            return
        if ev.kind == "wheel":
            self.zoom_at(1.15 if ev.button > 0 else 1 / 1.15, (px, py))
            return
        if ev.kind == "press":
            mode = "rotate"
            if ev.button in (1, 2) or ev.shift or ev.ctrl or ev.alt:
                mode = "pan"
            self.drag = dict(mode=mode, x=ev.x, y=ev.y, moved=False, t=now, v=[0.0, 0.0], button=ev.button)
            if mode == "rotate":
                self.omega = [0.0, 0.0]
                self.reset_anim = None
            return
        d = self.drag
        if d is None:
            return
        if ev.kind == "drag":
            dx, dy = ev.x - d["x"], ev.y - d["y"]
            if not dx and not dy:
                return
            d["moved"] = True
            d["x"], d["y"] = ev.x, ev.y
            if d["mode"] == "pan":
                self.pan[0] += dx
                self.pan[1] += 2 * dy
                self.reset_anim = None
            else:
                g = self.drag_gain()
                ay, ax = dx * g, 2 * dy * g
                self.q = q_norm(q_mul(q_axis(0, 1, 0, ay), q_mul(q_axis(1, 0, 0, ax), self.q)))
                dt = max(0.008, now - d["t"])
                a = 0.5
                d["v"] = [d["v"][0] * (1 - a) + ax / dt * a, d["v"][1] * (1 - a) + ay / dt * a]
                d["t"] = now
        elif ev.kind == "release":
            self.drag = None
            if d["mode"] == "rotate":
                if not d["moved"]:
                    self.spin(now)
                elif now - d["t"] < 0.08:
                    vx, vy = d["v"]
                    lim = 12.0
                    self.omega = [max(-lim, min(lim, vx)), max(-lim, min(lim, vy))]

    # ------------------------------------------------------------------ 主循环
    def suspend(self):
        self.term.stop()
        os.kill(os.getpid(), signal.SIGTSTP)
        self.term.start()
        self.screen.invalidate()
        self.dirty = True

    def check_resize(self):
        size = self.term.size()
        if size != (self.cols, self.rows):
            self.cols, self.rows = size
            self.screen.invalidate()
            self.dirty = True

    def run(self):
        next_frame = 0.0
        while self.running:
            now = time.monotonic()
            self.check_resize()
            if self.toast_text and now > self.toast_until:
                self.toast_text = None
                self.dirty = True
            if self.dirty or self.animating(now):
                if now >= next_frame:
                    self.update(now)
                    if self.cols > 0 and self.rows > 0:
                        self.draw(now)
                    self.dirty = False
                    next_frame = now + self.frame_interval()
                if self.dirty or self.animating(now):
                    timeout = max(0.0, next_frame - time.monotonic())
                else:
                    timeout = self.idle_timeout(now)
            else:
                self.last_update = now
                timeout = self.idle_timeout(now)
            events = self.term.read_events(timeout)
            now = time.monotonic()
            for ev in events:
                self.handle(ev, now)
                if not self.running:
                    break


# ====================================================================== 命令行
class Options(object):
    def __init__(self):
        self.logo = None
        self.help = False
        self.version = False
        self.list = False
        self.depth = None
        self.bg = "dark"
        self.bg_given = False
        self.grid = True
        self.intro = True
        self.auto = False
        self.fps = 60
        self.print = False
        self.size = None
        self.yaw = 0.0
        self.pitch = 0.0


OPTION_HELP = [
    ("-h, --help", "显示本帮助并退出"),
    ("-l, --list", "列出所有可用的 Logo 并退出"),
    ("-d, --depth N", "Logo 厚度（体素数，1-16）"),
    ("-b, --bg 主题", "背景：dark 深色渐变（默认）/ black 纯黑 / light 浅色 / term 终端默认"),
    ("--no-grid", "关闭像素网格线"),
    ("--no-intro", "启动时不播放旋转动画"),
    ("-a, --auto", "启动后自动旋转"),
    ("--fps N", "最高帧率（默认 60）"),
    ("-p, --print", "不进入交互界面，直接向标准输出打印一帧静态图像"),
    ("--size 宽x高", "配合 --print：图像大小（单位：字符），默认取终端大小"),
    ("--yaw 角度", "配合 --print：水平旋转角度"),
    ("--pitch 角度", "配合 --print：俯仰角度"),
    ("-V, --version", "显示版本号"),
]


def _pad(s, w):
    return s + " " * max(1, w - text_width(s))


def help_text(prog):
    lines = [
        "用法：%s [选项] [logo]" % prog,
        "",
        "在终端里渲染可以用鼠标交互的 3D 像素风 Logo。",
        "",
        "参数：",
        "  " + _pad("logo", 18) + "要显示的 Logo（名称、别名或序号）；省略则先打开选择列表",
        "",
        "选项：",
    ]
    for k, v in OPTION_HELP:
        lines.append("  " + _pad(k, 18) + v)
    lines += [
        "",
        "示例：",
        "  %s                 打开 Logo 列表" % prog,
        "  %s claude          直接显示 Claude 小螃蟹" % prog,
        "  %s 豆包 -b light    浅色背景显示豆包" % prog,
        "  %s vim -p --yaw 30 --pitch 15" % prog,
        "",
        "界面操作：左键单击 旋转一圈 | 左键拖动 3D 检视 | 右键拖动 平移 | 滚轮 缩放",
        "          Tab 打开列表 | H 帮助 | Q 退出",
        "",
        "使用 -l 查看全部 %d 个 Logo。" % len(LOGOS),
    ]
    return "\n".join(lines)


def list_text():
    lines = ["可用的 Logo（共 %d 个）：" % len(LOGOS), ""]
    kw = max(len(l.key) for l in LOGOS)
    nw = max(text_width(l.name) for l in LOGOS)
    for i, l in enumerate(LOGOS):
        lines.append("  %2d  %s%s%s" % (i + 1, l.key.ljust(kw + 2), _pad(l.name, nw + 2), ", ".join(l.aliases)))
    lines += ["", "名称、别名、序号都可以作为参数，例如：logo3d.py 鲸鱼  或  logo3d.py 6"]
    return "\n".join(lines)


class UsageError(Exception):
    pass


def parse_args(argv):
    o = Options()
    args = list(argv)
    pos = []

    def take_value(name, inline):
        if inline is not None:
            return inline
        if not args:
            raise UsageError("选项 %s 需要一个值" % name)
        return args.pop(0)

    while args:
        a = args.pop(0)
        if a == "--":
            pos.extend(args)
            break
        if not a.startswith("-") or a == "-":
            pos.append(a)
            continue
        inline = None
        if a.startswith("--") and "=" in a:
            a, inline = a.split("=", 1)
        if a in ("-h", "--help"):
            o.help = True
        elif a in ("-V", "--version"):
            o.version = True
        elif a in ("-l", "--list"):
            o.list = True
        elif a in ("-d", "--depth"):
            v = take_value(a, inline)
            try:
                o.depth = int(v)
            except ValueError:
                raise UsageError("厚度必须是整数：%s" % v)
            if not 1 <= o.depth <= 16:
                raise UsageError("厚度必须在 1 到 16 之间")
        elif a in ("-b", "--bg"):
            v = take_value(a, inline).lower()
            alias = {"深色": "dark", "黑": "black", "黑色": "black", "浅色": "light", "白": "light",
                     "终端": "term", "default": "term", "none": "term", "transparent": "term"}
            v = alias.get(v, v)
            if v not in THEMES:
                raise UsageError("未知的背景：%s（可选：%s）" % (v, ", ".join(THEMES)))
            o.bg = v
            o.bg_given = True
        elif a == "--no-grid":
            o.grid = False
        elif a == "--grid":
            o.grid = True
        elif a == "--no-intro":
            o.intro = False
        elif a in ("-a", "--auto"):
            o.auto = True
        elif a == "--fps":
            v = take_value(a, inline)
            try:
                o.fps = int(v)
            except ValueError:
                raise UsageError("帧率必须是整数：%s" % v)
        elif a in ("-p", "--print"):
            o.print = True
        elif a == "--size":
            v = take_value(a, inline).lower().replace("*", "x")
            try:
                w, h = [int(x) for x in v.split("x")]
                if w < 4 or h < 2:
                    raise ValueError
            except ValueError:
                raise UsageError("尺寸格式应为 宽x高，例如 80x24")
            o.size = (w, h)
        elif a in ("--yaw", "--pitch"):
            v = take_value(a, inline)
            try:
                setattr(o, a[2:], float(v))
            except ValueError:
                raise UsageError("角度必须是数字：%s" % v)
        else:
            raise UsageError("未知选项：%s" % a)
    if len(pos) > 1:
        raise UsageError("只能指定一个 Logo，收到了：%s" % " ".join(pos))
    if pos:
        o.logo = pos[0]
    return o


# ====================================================================== 静态输出
def print_frame(logo, o):
    import shutil
    if o.size:
        cols, rows = o.size
    else:
        sz = shutil.get_terminal_size((80, 24))
        cols, rows = sz.columns, max(8, min(sz.lines - 1, 30))
    m = Model(logo, o.depth)
    r = Renderer()
    theme = o.bg if o.bg_given else "term"
    r.configure(cols, rows * 2, theme)
    rot = mat_mul(rot_x(math.radians(o.pitch)), rot_y(math.radians(o.yaw)))
    scale = min(0.9 * cols / m.W, 0.88 * rows * 2 / m.H)
    cb = r.render(m, rot, cols / 2.0, rows, scale, o.grid)
    out = []
    for row in range(rows):
        a = 2 * row * cols
        out.append(encode_cells([(t << 25) | b for t, b in zip(cb[a:a + cols], cb[a + cols:a + 2 * cols])]))
    text = "\n".join(out) + "\n"
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.flush()


# ====================================================================== 入口
def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    prog = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else "logo3d"
    if prog in ("__main__.py", "-c", ""):
        prog = "python -m terminal_logo"
    try:
        o = parse_args(argv)
    except UsageError as e:
        sys.stderr.write("错误：%s\n使用 %s --help 查看帮助。\n" % (e, prog))
        return 2
    if o.help:
        print(help_text(prog))
        return 0
    if o.version:
        print("terminal-3d-logo %s" % __version__)
        return 0

    if o.list:
        print(list_text())
        return 0

    logo = None
    if o.logo is not None:
        logo = find_logo(o.logo)
        if logo is None:
            msg = "错误：找不到名为 “%s” 的 Logo。" % o.logo
            sug = suggest(o.logo)
            if sug:
                msg += "你是不是想找：%s？" % "、".join(sug)
            sys.stderr.write(msg + "\n使用 %s --list 查看全部 Logo。\n" % prog)
            return 2

    if o.print:
        print_frame(logo or LOGOS[0], o)
        return 0

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.stderr.write("错误：需要在交互式终端中运行（可以用 --print 输出静态图像）。\n")
        return 1

    term = Terminal()

    def on_signal(signum, frame):
        raise SystemExit(128 + signum)

    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), on_signal)
            except (ValueError, OSError):
                pass
    try:
        term.start()
        app = App(term, o, logo)
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        term.stop()
    return 0
