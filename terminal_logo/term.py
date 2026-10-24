# -*- coding: utf-8 -*-
"""终端控制：原始模式、备用缓冲区、鼠标上报、输入解析，以及按差异刷新的屏幕输出。"""

import codecs
import os
import shutil
import sys
import time
import unicodedata

from .render import DEFAULT_BG

IS_WINDOWS = os.name == "nt"

ESC_TIMEOUT = 0.03  # 单独的 ESC 键与转义序列开头的区分时间

ENTER_SEQ = (
    "\x1b[?1049h"   # 备用缓冲区
    "\x1b[?25l"     # 隐藏光标
    "\x1b[?7l"      # 关闭自动换行
    "\x1b[?1000h"   # 鼠标按键上报
    "\x1b[?1002h"   # 按住按键时的移动上报（拖动）
    "\x1b[?1006h"   # SGR 扩展坐标格式
    "\x1b[0m\x1b[2J"
)
EXIT_SEQ = (
    "\x1b[?1006l\x1b[?1002l\x1b[?1000l"
    "\x1b[0m\x1b[?7h\x1b[?25h"
    "\x1b[?1049l"
)


# ---------------------------------------------------------------------- 事件
class Key(object):
    """键盘事件。name 是特殊键名（up/down/left/right/enter/esc/tab/backtab/backspace/
    delete/home/end/pgup/pgdn/ctrl+x）或单个可打印字符。"""

    __slots__ = ("name", "shift", "ctrl", "alt")

    def __init__(self, name, shift=False, ctrl=False, alt=False):
        self.name = name
        self.shift = shift
        self.ctrl = ctrl
        self.alt = alt

    @property
    def is_char(self):
        return len(self.name) == 1

    def __repr__(self):
        return "Key(%r%s%s%s)" % (self.name, " shift" if self.shift else "", " ctrl" if self.ctrl else "",
                                  " alt" if self.alt else "")


class Mouse(object):
    """鼠标事件。kind: press / release / drag / move / wheel；button: 0 左 1 中 2 右；
    wheel 时 button 为 +1（向上）或 -1（向下）。x, y 为从 0 开始的单元格坐标。"""

    __slots__ = ("kind", "button", "x", "y", "shift", "ctrl", "alt")

    def __init__(self, kind, button, x, y, shift=False, ctrl=False, alt=False):
        self.kind = kind
        self.button = button
        self.x = x
        self.y = y
        self.shift = shift
        self.ctrl = ctrl
        self.alt = alt

    def __repr__(self):
        return "Mouse(%s b=%s %d,%d)" % (self.kind, self.button, self.x, self.y)


_CSI_TILDE = {"1": "home", "7": "home", "4": "end", "8": "end", "2": "insert", "3": "delete",
              "5": "pgup", "6": "pgdn"}
_CSI_FINAL = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end", "Z": "backtab",
              "P": "f1", "Q": "f2", "R": "f3", "S": "f4"}
_WIN_SCAN = {"H": "up", "P": "down", "K": "left", "M": "right", "G": "home", "O": "end", "I": "pgup",
             "Q": "pgdn", "S": "delete", "R": "insert", ";": "f1"}


class InputParser(object):
    """把终端输入的字符流解析成 Key / Mouse 事件。"""

    def __init__(self):
        self.buf = ""

    def feed(self, text, final=False):
        self.buf += text
        events = []
        buf = self.buf
        i = 0
        n = len(buf)
        while i < n:
            c = buf[i]
            if c == "\x1b":
                if i + 1 >= n:
                    if final:
                        events.append(Key("esc"))
                        i += 1
                        continue
                    break
                nxt = buf[i + 1]
                if nxt == "[" and i + 2 < n and buf[i + 2] == "M":
                    # 旧式 X10 鼠标上报：ESC [ M 后跟 3 个字节
                    if i + 6 > n:
                        if final:
                            i = n
                            continue
                        break
                    ev = self._x10(buf[i + 3:i + 6])
                    if ev is not None:
                        events.append(ev)
                    i += 6
                elif nxt == "[":
                    j = i + 2
                    while j < n and not ("\x40" <= buf[j] <= "\x7e"):
                        j += 1
                    if j >= n:
                        if final:  # 不完整的序列，丢弃
                            i = n
                            continue
                        break
                    ev = self._csi(buf[i + 2:j], buf[j])
                    if ev is not None:
                        events.append(ev)
                    i = j + 1
                elif nxt == "O":
                    if i + 2 >= n:
                        if final:
                            i = n
                            continue
                        break
                    name = _CSI_FINAL.get(buf[i + 2])
                    if name:
                        events.append(Key(name))
                    i += 3
                elif nxt == "\x1b":
                    events.append(Key("esc"))
                    i += 1
                else:
                    k = self._plain(nxt)
                    k.alt = True
                    events.append(k)
                    i += 2
            elif IS_WINDOWS and c in "\x00\xe0":
                if i + 1 >= n:
                    if final:
                        i += 1
                        continue
                    break
                name = _WIN_SCAN.get(buf[i + 1])
                if name:
                    events.append(Key(name))
                i += 2
            else:
                events.append(self._plain(c))
                i += 1
        self.buf = buf[i:]
        return events

    @staticmethod
    def _plain(c):
        if c in "\r\n":
            return Key("enter")
        if c == "\t":
            return Key("tab")
        if c in "\x7f\x08":
            return Key("backspace")
        if c == "\x03":
            return Key("ctrl+c", ctrl=True)
        if c < " ":
            return Key("ctrl+" + chr(ord(c) + 96), ctrl=True)
        return Key(c)

    @staticmethod
    def _x10(data):
        b, x, y = ord(data[0]) - 32, ord(data[1]) - 33, ord(data[2]) - 33
        shift, alt, ctrl = bool(b & 4), bool(b & 8), bool(b & 16)
        if b & 64:
            return Mouse("wheel", 1 if (b & 3) == 0 else -1, x, y, shift, ctrl, alt)
        button = b & 3
        if b & 32:
            return Mouse("drag", button, x, y, shift, ctrl, alt) if button != 3 else None
        if button == 3:
            return Mouse("release", 0, x, y, shift, ctrl, alt)
        return Mouse("press", button, x, y, shift, ctrl, alt)

    @staticmethod
    def _csi(params, final):
        if params.startswith("<") and final in "Mm":
            try:
                b, x, y = [int(p) for p in params[1:].split(";")]
            except ValueError:
                return None
            shift, alt, ctrl = bool(b & 4), bool(b & 8), bool(b & 16)
            x -= 1
            y -= 1
            if b & 64:
                code = b & 3
                if code in (0, 1):
                    return Mouse("wheel", 1 if code == 0 else -1, x, y, shift, ctrl, alt)
                return None
            button = b & 3
            if b & 32:
                return Mouse("drag" if button != 3 else "move", button, x, y, shift, ctrl, alt)
            if final == "m":
                return Mouse("release", button, x, y, shift, ctrl, alt)
            if button == 3:  # 旧式“松开”编码
                return Mouse("release", 0, x, y, shift, ctrl, alt)
            return Mouse("press", button, x, y, shift, ctrl, alt)
        parts = params.split(";")
        mod = 0
        if len(parts) > 1 and parts[1].isdigit():
            mod = int(parts[1]) - 1
        shift, alt, ctrl = bool(mod & 1), bool(mod & 2), bool(mod & 4)
        if final == "~":
            name = _CSI_TILDE.get(parts[0])
            return Key(name, shift, ctrl, alt) if name else None
        name = _CSI_FINAL.get(final)
        if name:
            return Key(name, shift or name == "backtab", ctrl, alt)
        return None


# ---------------------------------------------------------------------- 终端
class Terminal(object):
    """进入/退出交互模式，读取输入，写出数据。"""

    def __init__(self):
        self.active = False
        self.parser = InputParser()
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._old = None
        self._win_modes = None
        self.out = sys.stdout

    # --- 生命周期 ---------------------------------------------------------
    def start(self):
        if IS_WINDOWS:
            self._win_start()
        else:
            import termios
            import tty
            fd = sys.stdin.fileno()
            self._old = termios.tcgetattr(fd)
            tty.setraw(fd, termios.TCSANOW)
        self.active = True
        self.write(ENTER_SEQ)
        self.flush()

    def stop(self):
        if not self.active:
            return
        self.active = False
        try:
            self.write(EXIT_SEQ)
            self.flush()
        except Exception:
            pass
        if IS_WINDOWS:
            self._win_stop()
        elif self._old is not None:
            import termios
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old)
            except Exception:
                pass

    def size(self):
        try:
            sz = os.get_terminal_size(sys.stdout.fileno())
            return sz.columns, sz.lines
        except OSError:
            sz = shutil.get_terminal_size((80, 24))
            return sz.columns, sz.lines

    # --- 输出 -------------------------------------------------------------
    def write(self, s):
        self.out.write(s)

    def flush(self):
        self.out.flush()

    # --- 输入 -------------------------------------------------------------
    def read_events(self, timeout):
        """等待最多 timeout 秒，返回期间收到的全部事件。"""
        if IS_WINDOWS:
            text = self._win_read(timeout)
        else:
            text = self._posix_read(timeout)
        events = self.parser.feed(text) if text else []
        if self.parser.buf:
            # 可能是单独的 ESC，也可能是被拆开的转义序列：稍等一下再判断
            more = self._win_read(ESC_TIMEOUT) if IS_WINDOWS else self._posix_read(ESC_TIMEOUT)
            events += self.parser.feed(more, final=not more)
        return events

    def _posix_read(self, timeout):
        import select
        fd = sys.stdin.fileno()
        try:
            r, _, _ = select.select([fd], [], [], max(0.0, timeout))
        except (InterruptedError, OSError):
            return ""
        if not r:
            return ""
        chunks = []
        while True:
            try:
                data = os.read(fd, 65536)
            except (InterruptedError, BlockingIOError):
                break
            if not data:
                break
            chunks.append(data)
            r, _, _ = select.select([fd], [], [], 0)
            if not r:
                break
        return self._decoder.decode(b"".join(chunks))

    # --- Windows ----------------------------------------------------------
    def _win_start(self):
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        hin = k32.GetStdHandle(-10)
        hout = k32.GetStdHandle(-11)
        mode_in = wintypes.DWORD()
        mode_out = wintypes.DWORD()
        k32.GetConsoleMode(hin, ctypes.byref(mode_in))
        k32.GetConsoleMode(hout, ctypes.byref(mode_out))
        self._win_modes = (hin, hout, mode_in.value, mode_out.value)
        ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
        ENABLE_EXTENDED_FLAGS = 0x0080
        k32.SetConsoleMode(hin, ENABLE_VIRTUAL_TERMINAL_INPUT | ENABLE_EXTENDED_FLAGS)
        ENABLE_PROCESSED_OUTPUT = 0x0001
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        k32.SetConsoleMode(hout, mode_out.value | ENABLE_PROCESSED_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        try:
            k32.SetConsoleOutputCP(65001)
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    def _win_stop(self):
        if not self._win_modes:
            return
        import ctypes
        hin, hout, mi, mo = self._win_modes
        ctypes.windll.kernel32.SetConsoleMode(hin, mi)
        ctypes.windll.kernel32.SetConsoleMode(hout, mo)

    def _win_read(self, timeout):
        import msvcrt
        end = time.time() + max(0.0, timeout)
        chars = []
        while True:
            while msvcrt.kbhit():
                chars.append(msvcrt.getwch())
            if chars or time.time() >= end:
                break
            time.sleep(0.004)
        return "".join(chars)


# ---------------------------------------------------------------------- 文本宽度
def char_width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def text_width(s):
    return sum(char_width(c) for c in s)


def clip_text(s, width):
    """截断到不超过 width 列。"""
    out = []
    w = 0
    for c in s:
        cw = char_width(c)
        if w + cw > width:
            break
        out.append(c)
        w += cw
    return "".join(out)


# ---------------------------------------------------------------------- 屏幕
class Screen(object):
    """把“单元格行”转换成 ANSI 输出，只重绘与上一帧不同的部分。

    单元格可以是：
      * int：像素对 (上半像素颜色 << 25) | 下半像素颜色，用 ▀ 输出；
      * tuple (字符, 前景色, 背景色)：文字；
      * None：宽字符（中文）的第二列占位。
    颜色是 0xRRGGBB，DEFAULT_BG 表示终端默认颜色。
    """

    def __init__(self):
        self.prev = None
        self.fg = None
        self.bg = None
        self._fg_codes = {}
        self._bg_codes = {}

    def invalidate(self):
        self.prev = None

    def _fg(self, c):
        s = self._fg_codes.get(c)
        if s is None:
            if c == DEFAULT_BG:
                s = "39"
            else:
                s = "38;2;%d;%d;%d" % ((c >> 16) & 255, (c >> 8) & 255, c & 255)
            if len(self._fg_codes) > 60000:
                self._fg_codes.clear()
            self._fg_codes[c] = s
        return s

    def _bg(self, c):
        s = self._bg_codes.get(c)
        if s is None:
            if c == DEFAULT_BG:
                s = "49"
            else:
                s = "48;2;%d;%d;%d" % ((c >> 16) & 255, (c >> 8) & 255, c & 255)
            if len(self._bg_codes) > 60000:
                self._bg_codes.clear()
            self._bg_codes[c] = s
        return s

    def diff(self, rows):
        """返回把屏幕更新为 rows 所需的转义序列字符串。"""
        out = []
        prev = self.prev
        if prev is None or len(prev) != len(rows) or (rows and len(prev[0]) != len(rows[0])):
            out.append("\x1b[0m\x1b[2J")
            self.fg = self.bg = None
            prev = None
        fg, bg = self.fg, self.bg
        _fg, _bg = self._fg, self._bg
        for r, row in enumerate(rows):
            if prev is not None:
                old = prev[r]
                if old == row:
                    continue
                n = len(row)
                c0 = 0
                while c0 < n and row[c0] == old[c0]:
                    c0 += 1
                c1 = n - 1
                while c1 > c0 and row[c1] == old[c1]:
                    c1 -= 1
                # 不要从宽字符的第二列开始，也不要把旧宽字符拆开
                while c0 > 0 and (row[c0] is None or old[c0] is None):
                    c0 -= 1
                while c1 + 1 < n and (row[c1 + 1] is None or old[c1 + 1] is None):
                    c1 += 1
            else:
                c0, c1 = 0, len(row) - 1
            out.append("\x1b[%d;%dH" % (r + 1, c0 + 1))
            for c in range(c0, c1 + 1):
                cell = row[c]
                if cell is None:
                    continue
                if cell.__class__ is int:
                    top = cell >> 25
                    bot = cell & 0x1FFFFFF
                    if top == bot:
                        if bot != bg:
                            bg = bot
                            out.append("\x1b[" + _bg(bg) + "m")
                        out.append(" ")
                        continue
                    if top == DEFAULT_BG:
                        ch, f, b = "▄", bot, top
                    else:
                        ch, f, b = "▀", top, bot
                else:
                    ch, f, b = cell
                    if ch == " ":
                        if b != bg:
                            bg = b
                            out.append("\x1b[" + _bg(bg) + "m")
                        out.append(" ")
                        continue
                if f != fg and b != bg:
                    fg, bg = f, b
                    out.append("\x1b[" + _fg(f) + ";" + _bg(b) + "m")
                elif f != fg:
                    fg = f
                    out.append("\x1b[" + _fg(f) + "m")
                elif b != bg:
                    bg = b
                    out.append("\x1b[" + _bg(b) + "m")
                out.append(ch)
        self.fg, self.bg = fg, bg
        self.prev = rows
        return "".join(out)


def encode_cells(cells):
    """把一行像素单元格编码成独立的一行 ANSI 文本（用于 --print）。"""
    sc = Screen()
    s = sc.diff([cells])
    s = s[s.index("H") + 1:]  # 去掉清屏和光标定位
    return s + "\x1b[0m"
