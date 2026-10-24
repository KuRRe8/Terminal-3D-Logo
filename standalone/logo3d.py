#!/usr/bin/env python3
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

__version__ = "1.0.0"

import codecs
import difflib
import math
import os
import shutil
import signal
import sys
import time
import unicodedata


# ======================================================================
# model.py
# ======================================================================
"""把像素画挤出成 3D 体素模型。

模型由若干矩形面组成：
* 顶/底“盖面”（法线 ±z）：同一高度的像素合并成一个大矩形，逐像素查表取色；
* 侧壁（法线 ±x / ±y）：相邻像素高度不同处的竖直面，连续的边会合并成长条。

坐标系：x 向右，y 向上，z 指向观察者；模型中心在原点，一个体素边长为 1。
"""


# 法线编号
NX_POS, NX_NEG, NY_POS, NY_NEG, NZ_POS, NZ_NEG = range(6)
NORMALS = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]


def hex_to_rgb(h):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


class Face(object):
    """一个矩形面：P(u, v) = origin + u * udir + v * vdir，u∈[0, ulen]，v∈[0, vlen]。

    kind == "cap"  : lut 为 (vlen+1) 行 × (ulen+1) 列的调色板下标表（-1 表示空），
                     多出的一行一列是为了容忍浮点误差。
    kind == "wall" : lut 为长度 ulen+1 的调色板下标表（沿 u 方向逐格取色）。
    """

    __slots__ = ("kind", "normal", "axis", "coord", "origin", "udir", "vdir", "ulen", "vlen",
                 "lut", "stride", "voff")

    def __init__(self, kind, normal, origin, udir, vdir, ulen, vlen, lut, stride=0, voff=0.0):
        self.kind = kind
        self.normal = normal
        n = NORMALS[normal]
        self.axis = 0 if n[0] else (1 if n[1] else 2)
        self.coord = origin[self.axis]
        self.origin = origin
        self.udir = udir
        self.vdir = vdir
        self.ulen = ulen
        self.vlen = vlen
        self.lut = lut
        self.stride = stride
        self.voff = voff  # 侧壁网格线的 z 相位


class Model(object):
    def __init__(self, logo, depth=None):
        self.logo = logo
        W, H = logo.width, logo.height
        if depth is None:
            depth = logo.depth or max(3, int(round(max(W, H) / 8.0)))
        self.depth = depth
        chars = list(logo.palette)
        colors = [hex_to_rgb(logo.palette[c][0]) for c in chars]
        relief = [int(logo.palette[c][1]) for c in chars]
        self.colors = colors  # 调色板（不含光照）
        n = len(colors)
        self.ncolors = n

        half = depth / 2.0
        cell = [[-1] * W for _ in range(H)]   # 正面颜色下标
        cellb = [[-1] * W for _ in range(H)]  # 背面颜色下标
        top = [[0.0] * W for _ in range(H)]
        bot = [[0.0] * W for _ in range(H)]
        back_map = logo.back_map
        for j, row in enumerate(logo.rows):
            for i, ch in enumerate(row):
                if ch == ".":
                    continue
                p = chars.index(ch)
                pb = chars.index(back_map.get(ch, ch))
                cell[j][i] = p
                cellb[j][i] = pb
                top[j][i] = half + relief[p]
                bot[j][i] = -(half + relief[pb])
        self.W, self.H = W, H
        x0 = -W / 2.0
        y0 = H / 2.0
        zmin = min(bot[j][i] for j in range(H) for i in range(W) if cell[j][i] >= 0)
        zmax = max(top[j][i] for j in range(H) for i in range(W) if cell[j][i] >= 0)
        self.zmin, self.zmax = zmin, zmax
        self.radius = 0.5 * math.sqrt(W * W + H * H + (zmax - zmin) ** 2)

        faces = []

        # ---------------------------------------------------------------- 盖面
        def build_caps(level_of, normal, ao_test):
            levels = sorted(set(level_of[j][i] for j in range(H) for i in range(W) if cell[j][i] >= 0))
            for lv in levels:
                cells = [(j, i) for j in range(H) for i in range(W)
                         if cell[j][i] >= 0 and level_of[j][i] == lv]
                j0 = min(c[0] for c in cells)
                j1 = max(c[0] for c in cells)
                i0 = min(c[1] for c in cells)
                i1 = max(c[1] for c in cells)
                cw, ch = i1 - i0 + 1, j1 - j0 + 1
                stride = cw + 1
                lut = [-1] * (stride * (ch + 1))
                for (j, i) in cells:
                    p = cell[j][i] if normal == NZ_POS else cellb[j][i]
                    # 环境光遮蔽：紧挨着更高浮雕的格子稍暗一些
                    ao = 0
                    for dj in (-1, 0, 1):
                        for di in (-1, 0, 1):
                            jj, ii = j + dj, i + di
                            if 0 <= jj < H and 0 <= ii < W and cell[jj][ii] >= 0 and ao_test(level_of[jj][ii], lv):
                                ao = 1
                    lut[(j - j0) * stride + (i - i0)] = p + n * ao
                # 浮点误差保护：多出的一列/一行复制边缘
                for r in range(ch):
                    lut[r * stride + cw] = lut[r * stride + cw - 1]
                for c in range(stride):
                    lut[ch * stride + c] = lut[(ch - 1) * stride + c]
                # u 沿 +x，v 沿 -y（按像素画的行列方向）；背面只是 z 与法线不同
                origin = (x0 + i0, y0 - j0, lv)
                faces.append(Face("cap", normal, origin, (1.0, 0.0, 0.0), (0.0, -1.0, 0.0), cw, ch, lut, stride))

        build_caps(top, NZ_POS, lambda other, me: other > me)
        build_caps(bot, NZ_NEG, lambda other, me: other < me)

        # ---------------------------------------------------------------- 侧壁
        def rng(j, i):
            if 0 <= j < H and 0 <= i < W and cell[j][i] >= 0:
                return (bot[j][i], top[j][i])
            return None

        def exposed(a, b):
            """a 区间里没有被 b 挡住的部分。"""
            if a is None:
                return []
            if b is None:
                return [a]
            out = []
            if a[0] < b[0]:
                out.append((a[0], min(a[1], b[0])))
            if b[1] < a[1]:
                out.append((max(a[0], b[1]), a[1]))
            return [s for s in out if s[1] - s[0] > 1e-9]

        # segs[key] = {line_index: color}；key = (normal, boundary, z0, z1)
        segs = {}

        def add_seg(normal, boundary, along, z0, z1, j, i):
            # 完全位于背面一半的侧壁用背面颜色，其余用正面颜色
            color = cellb[j][i] if z1 <= 0 else cell[j][i]
            segs.setdefault((normal, boundary, z0, z1), {})[along] = color

        for j in range(H):
            for i in range(W + 1):
                a = rng(j, i - 1)  # 左边的格子
                b = rng(j, i)      # 右边的格子
                for (z0, z1) in exposed(a, b):
                    add_seg(NX_POS, i, j, z0, z1, j, i - 1)
                for (z0, z1) in exposed(b, a):
                    add_seg(NX_NEG, i, j, z0, z1, j, i)
        for j in range(H + 1):
            for i in range(W):
                a = rng(j - 1, i)  # 上面的格子
                b = rng(j, i)      # 下面的格子
                for (z0, z1) in exposed(a, b):
                    add_seg(NY_NEG, j, i, z0, z1, j - 1, i)
                for (z0, z1) in exposed(b, a):
                    add_seg(NY_POS, j, i, z0, z1, j, i)

        for (normal, boundary, z0, z1), runs in segs.items():
            keys = sorted(runs)
            start = 0
            while start < len(keys):
                end = start
                while end + 1 < len(keys) and keys[end + 1] == keys[end] + 1:
                    end += 1
                a0 = keys[start]
                cnt = keys[end] - a0 + 1
                lut = [runs[k] for k in range(a0, a0 + cnt)]
                lut.append(lut[-1])
                voff = z0 + half
                if normal in (NX_POS, NX_NEG):
                    origin = (x0 + boundary, y0 - a0, z0)
                    udir = (0.0, -1.0, 0.0)
                else:
                    origin = (x0 + a0, y0 - boundary, z0)
                    udir = (1.0, 0.0, 0.0)
                faces.append(Face("wall", normal, origin, udir, (0.0, 0.0, 1.0), cnt, z1 - z0, lut, 0, voff))
                start = end + 1

        self.faces = faces
        self.ncaps = sum(1 for f in faces if f.kind == "cap")
        self.nwalls = len(faces) - self.ncaps


# ======================================================================
# render.py
# ======================================================================
"""纯 Python 软件光栅化渲染器。

每个矩形面按透视投影到屏幕上，逐扫描线求出覆盖范围，再用逆单应矩阵把屏幕像素
映射回面上的 (u, v) 坐标查表取色。深度缓冲存的是 1/深度（越大越近）。
输出是一维像素数组，颜色为 0xRRGGBB 整数；DEFAULT_BG 表示“终端默认背景色”。
"""



DEFAULT_BG = 1 << 24  # 特殊值：不输出背景色，使用终端自己的背景

# 摄像机空间中的光照方向（从左上前方照过来）
_L = (-0.45, 0.50, 0.74)
_ln = math.sqrt(sum(c * c for c in _L))
LIGHT = tuple(c / _ln for c in _L)
_H = (LIGHT[0], LIGHT[1], LIGHT[2] + 1.0)
_hn = math.sqrt(sum(c * c for c in _H))
HALF = tuple(c / _hn for c in _H)
AMBIENT = 0.32
DIFFUSE = 0.68
REST = AMBIENT + DIFFUSE * LIGHT[2]  # 正对观察者时的亮度，归一化为 1
SPECULAR = 0.30
SHININESS = 24.0
AO_FACTOR = 0.80
LINE_DARKEN = 0.13  # 网格线最暗时的变暗比例

THEMES = ["dark", "black", "light", "term"]
THEME_NAMES = {"dark": "深色渐变", "black": "纯黑", "light": "浅色", "term": "终端默认"}


def mat_mul(a, b):
    return (
        a[0] * b[0] + a[1] * b[3] + a[2] * b[6], a[0] * b[1] + a[1] * b[4] + a[2] * b[7], a[0] * b[2] + a[1] * b[5] + a[2] * b[8],
        a[3] * b[0] + a[4] * b[3] + a[5] * b[6], a[3] * b[1] + a[4] * b[4] + a[5] * b[7], a[3] * b[2] + a[4] * b[5] + a[5] * b[8],
        a[6] * b[0] + a[7] * b[3] + a[8] * b[6], a[6] * b[1] + a[7] * b[4] + a[8] * b[7], a[6] * b[2] + a[7] * b[5] + a[8] * b[8],
    )


def rot_y(a):
    c, s = math.cos(a), math.sin(a)
    return (c, 0.0, s, 0.0, 1.0, 0.0, -s, 0.0, c)


def rot_x(a):
    c, s = math.cos(a), math.sin(a)
    return (1.0, 0.0, 0.0, 0.0, c, -s, 0.0, s, c)


def _lerp(a, b, t):
    return a + (b - a) * t


def _mix(c0, c1, t):
    return (int(round(_lerp(c0[0], c1[0], t))) << 16) | (int(round(_lerp(c0[1], c1[1], t))) << 8) | int(round(_lerp(c0[2], c1[2], t)))


def make_background(theme, pw, ph):
    """生成背景像素数组。为了让终端输出更省，同一行颜色相同（竖直渐变）。"""
    if theme == "term":
        return [DEFAULT_BG] * (pw * ph)
    if theme == "black":
        return [0] * (pw * ph)
    if theme == "light":
        top, bottom = (0xF4, 0xF5, 0xF8), (0xD9, 0xDD, 0xE4)
    else:
        top, bottom = (0x2A, 0x2E, 0x39), (0x12, 0x14, 0x19)
    out = []
    for y in range(ph):
        t = y / float(max(1, ph - 1))
        t = t * t * (3 - 2 * t)
        out.extend([_mix(top, bottom, t)] * pw)
    return out


class Renderer(object):
    def __init__(self):
        self.pw = 0
        self.ph = 0
        self.theme = "dark"
        self.bg = []
        self.stats = (0, 0)  # 上一帧绘制的面数、像素数

    def configure(self, pw, ph, theme):
        if (pw, ph, theme) != (self.pw, self.ph, self.theme) or not self.bg:
            self.pw, self.ph, self.theme = pw, ph, theme
            self.bg = make_background(theme, pw, ph)

    # ------------------------------------------------------------------ 光照
    def _palettes(self, model, rot, line_factor):
        """为 6 个法线方向分别生成带光照的调色板：下标 p + n*ao + 2n*line。"""
        n = model.ncolors
        cols = model.colors
        pals = []
        for k, nv in enumerate(NORMALS):
            # 摄像机空间法线 = R · n（R 的某一列）
            nx = rot[0] * nv[0] + rot[1] * nv[1] + rot[2] * nv[2]
            ny = rot[3] * nv[0] + rot[4] * nv[1] + rot[5] * nv[2]
            nz = rot[6] * nv[0] + rot[7] * nv[1] + rot[8] * nv[2]
            d = nx * LIGHT[0] + ny * LIGHT[1] + nz * LIGHT[2]
            inten = (AMBIENT + DIFFUSE * max(0.0, d)) / REST
            h = nx * HALF[0] + ny * HALF[1] + nz * HALF[2]
            spec = SPECULAR * (max(0.0, h) ** SHININESS) * 255.0
            pal = [0] * (4 * n)
            for variant in range(4):
                f = inten
                if variant & 1:
                    f *= AO_FACTOR
                if variant & 2:
                    f *= line_factor
                sp = spec * (0.5 if variant else 1.0)
                for p in range(n):
                    r, g, b = cols[p]
                    r = int(r * f + sp)
                    g = int(g * f + sp)
                    b = int(b * f + sp)
                    if r > 255:
                        r = 255
                    if g > 255:
                        g = 255
                    if b > 255:
                        b = 255
                    pal[p + variant * n] = (r << 16) | (g << 8) | b
            pals.append(pal)
        return pals

    # ------------------------------------------------------------------ 渲染
    def render(self, model, rot, cx, cy, scale, grid=True):
        """rot: 3x3 旋转矩阵（行优先）；(cx, cy): 模型中心在屏幕上的像素坐标；
        scale: 模型中心深度处一个体素对应的像素数。返回像素数组。"""
        pw, ph = self.pw, self.ph
        cb = list(self.bg)
        if pw <= 0 or ph <= 0 or scale <= 0:
            return cb
        zb = [0.0] * (pw * ph)
        dist = model.radius * 3.2
        ax = scale * dist
        ay = scale * dist
        n = model.ncolors
        # 体素足够大时才画网格线，并随大小渐显，避免小尺寸下出现杂乱的纹路
        strength = min(1.0, max(0.0, (scale - 4.0) / 4.0)) if grid else 0.0
        pals = self._palettes(model, rot, 1.0 - LINE_DARKEN * strength)
        # 摄像机在物体空间中的位置，用于背面剔除
        cam = (rot[6] * dist, rot[7] * dist, rot[8] * dist)
        r0, r1, r2, r3, r4, r5, r6, r7, r8 = rot
        grid_w = 1.0 / scale if strength > 0 else 0.0
        floor = math.floor
        ceil = math.ceil
        nfaces = 0
        npix = 0
        for f in model.faces:
            sign = 1.0 if f.normal % 2 == 0 else -1.0
            if (cam[f.axis] - f.coord) * sign <= 1e-9:
                continue
            ox, oy, oz = f.origin
            ux, uy, uz = f.udir
            vx, vy, vz = f.vdir
            # 物体空间 -> 摄像机空间
            o_x = r0 * ox + r1 * oy + r2 * oz
            o_y = r3 * ox + r4 * oy + r5 * oz
            o_z = r6 * ox + r7 * oy + r8 * oz
            a_x = r0 * ux + r1 * uy + r2 * uz
            a_y = r3 * ux + r4 * uy + r5 * uz
            a_z = r6 * ux + r7 * uy + r8 * uz
            b_x = r0 * vx + r1 * vy + r2 * vz
            b_y = r3 * vx + r4 * vy + r5 * vz
            b_z = r6 * vx + r7 * vy + r8 * vz
            # 正向单应矩阵 (u, v, 1) -> (sx*w, sy*w, w)
            w0, w1, w2 = -a_z, -b_z, dist - o_z
            h00 = cx * w0 + ax * a_x
            h01 = cx * w1 + ax * b_x
            h02 = cx * w2 + ax * o_x
            h10 = cy * w0 - ay * a_y
            h11 = cy * w1 - ay * b_y
            h12 = cy * w2 - ay * o_y
            ul, vl = f.ulen, f.vlen
            # 四个角的屏幕坐标
            pts = []
            for (u, v) in ((0.0, 0.0), (ul, 0.0), (ul, vl), (0.0, vl)):
                w = w0 * u + w1 * v + w2
                if w <= 1e-6:
                    pts = None
                    break
                pts.append(((h00 * u + h01 * v + h02) / w, (h10 * u + h11 * v + h12) / w))
            if not pts:
                continue
            ys = [p[1] for p in pts]
            ymin, ymax = min(ys), max(ys)
            ya = int(ceil(ymin - 0.5))
            yb = int(floor(ymax - 0.5))
            if ya < 0:
                ya = 0
            if yb > ph - 1:
                yb = ph - 1
            if ya > yb:
                continue
            xs_all = [p[0] for p in pts]
            if max(xs_all) < 0 or min(xs_all) > pw:
                continue
            # 逆矩阵（伴随矩阵 / 行列式）
            det = (h00 * (h11 * w2 - h12 * w1) - h01 * (h10 * w2 - h12 * w0) + h02 * (h10 * w1 - h11 * w0))
            if abs(det) < 1e-12:
                continue
            idt = 1.0 / det
            i00 = (h11 * w2 - h12 * w1) * idt
            i01 = (h02 * w1 - h01 * w2) * idt
            i02 = (h01 * h12 - h02 * h11) * idt
            i10 = (h12 * w0 - h10 * w2) * idt
            i11 = (h00 * w2 - h02 * w0) * idt
            i12 = (h02 * h10 - h00 * h12) * idt
            i20 = (h10 * w1 - h11 * w0) * idt
            i21 = (h01 * w0 - h00 * w1) * idt
            i22 = (h00 * h11 - h01 * h10) * idt
            edges = []
            for k in range(4):
                xa_, ya_ = pts[k]
                xb_, yb_ = pts[(k + 1) & 3]
                if ya_ != yb_:
                    if ya_ > yb_:
                        xa_, ya_, xb_, yb_ = xb_, yb_, xa_, ya_
                    edges.append((ya_, yb_, xa_, (xb_ - xa_) / (yb_ - ya_)))
            pal = pals[f.normal]
            lut = f.lut
            is_cap = f.kind == "cap"
            stride = f.stride
            voff = f.voff + 64.0
            gw = grid_w
            off_line = 2 * n
            nfaces += 1
            for py in range(ya, yb + 1):
                fy = py + 0.5
                xl = 1e18
                xr = -1e18
                for (e0, e1, ex, slope) in edges:
                    if e0 <= fy <= e1:
                        x = ex + (fy - e0) * slope
                        if x < xl:
                            xl = x
                        if x > xr:
                            xr = x
                if xl > xr:
                    continue
                xa = int(ceil(xl - 0.5))
                xb = int(floor(xr - 0.5))
                if xa < 0:
                    xa = 0
                if xb > pw - 1:
                    xb = pw - 1
                if xa > xb:
                    continue
                fx = xa + 0.5
                A = i00 * fx + i01 * fy + i02
                B = i10 * fx + i11 * fy + i12
                C = i20 * fx + i21 * fy + i22
                base = py * pw
                npix += xb - xa + 1
                if is_cap:
                    for idx in range(base + xa, base + xb + 1):
                        if C > zb[idx]:
                            k = 1.0 / C
                            u = A * k
                            v = B * k
                            iu = int(u)
                            iv = int(v)
                            p = lut[iv * stride + iu]
                            if p >= 0:
                                zb[idx] = C
                                if gw and (u - iu < gw or v - iv < gw):
                                    p += off_line
                                cb[idx] = pal[p]
                        A += i00
                        B += i10
                        C += i20
                else:
                    for idx in range(base + xa, base + xb + 1):
                        if C > zb[idx]:
                            k = 1.0 / C
                            u = A * k
                            iu = int(u)
                            zb[idx] = C
                            p = lut[iu]
                            if gw:
                                t = B * k + voff
                                if u - iu < gw or t - int(t) < gw:
                                    p += off_line
                            cb[idx] = pal[p]
                        A += i00
                        B += i10
                        C += i20
        self.stats = (nfaces, npix)
        return cb


# ======================================================================
# term.py
# ======================================================================
"""终端控制：原始模式、备用缓冲区、鼠标上报、输入解析，以及按差异刷新的屏幕输出。"""



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


# ======================================================================
# logos.py
# ======================================================================
"""Logo 数据。

每个 logo 都是一张像素画：``art`` 中每个字符是一个像素，``.`` 表示空白，
其余字符通过 ``palette`` 映射为 ``(颜色, 浮雕高度)``。渲染时像素画会被挤出成
体素（3D 方块），浮雕高度表示该颜色的像素比底板凸出（正数）或凹进（负数）
多少个体素。

``depth`` 为底板厚度（体素数，缺省按尺寸自动计算）。``back_map`` 可以把背面
的某些字符替换成别的字符（例如豆包的背面把脸换成头发、小螃蟹的背面没有眼睛），
没有替换的字符背面与正面相同（镜像）。
"""



class Logo(object):
    __slots__ = ("key", "name", "aliases", "palette", "rows", "width", "height", "depth", "back_map")

    def __init__(self, key, name, aliases, palette, art, depth=None, back_map=None):
        self.key = key
        self.name = name
        self.aliases = tuple(aliases)
        self.palette = palette
        self.rows = [r for r in art.strip("\n").split("\n")]
        self.width = max(len(r) for r in self.rows)
        self.height = len(self.rows)
        self.rows = [r.ljust(self.width, ".") for r in self.rows]
        self.depth = depth
        self.back_map = dict(back_map or {})
        for r in self.rows:
            for ch in r:
                if ch != "." and ch not in palette:
                    raise ValueError("logo %s: 调色板缺少字符 %r" % (key, ch))
        for a, b in self.back_map.items():
            if b not in palette:
                raise ValueError("logo %s: back_map 指向未知字符 %r" % (key, b))

    def __repr__(self):
        return "<Logo %s %dx%d>" % (self.key, self.width, self.height)


LOGOS = [
    Logo(
        'claude', 'Claude 小螃蟹', ('clawd', 'claude-code', 'claudecode', 'cc', 'crab', 'anthropic', '螃蟹', '小螃蟹'),
        palette={'o': ('#D97757', 0), 'k': ('#2A1F1A', -1)},
        depth=6, back_map={'k': 'o'},
        art="""
..oooooooooooo..
..oooooooooooo..
..ookooooookoo..
..ookooooookoo..
oooooooooooooooo
oooooooooooooooo
..oooooooooooo..
..oooooooooooo..
...o.o....o.o...
...o.o....o.o...
"""),
    Logo(
        'codex', 'Codex', ('openai', 'openai-codex', 'codex-cli'),
        palette={'a': ('#4A5CF8', 0), 'b': ('#6880F8', 0), 'c': ('#7E98F8', 0), 'd': ('#94A0F8', 0), 'e': ('#A8A6F8', 0), 'w': ('#F8F8F8', 1)},
        art="""
..........eeeeee..............
........eeeeeeeeee............
.......eeeeeeeeeeeeeeee.......
.......eeeeeeeeeeeeeeeeee.....
......eeeeeeeeeeeeeeeeeeee....
......ddddddddddddddddddddd...
....ddddddddddddddddddddddd...
..dddddddddddddddddddddddddd..
.ddddddddddddddddddddddddddd..
.ddddddddddddddddddddddddddd..
dddddddwwddddddddddddddddddd..
cccccccwwwcccccccccccccccccc..
ccccccccwwccccccccccccccccccc.
ccccccccwwwcccccccccccccccccc.
cccccccccwwccccccccccccccccccc
cccccccccwwwcccccccccccccccccc
.ccccccccwwccccccccccccccccccc
.bbbbbbbwwbbbbbbbbbbbbbbbbbbbb
..bbbbbwwwbbbbbwwwwwwwwbbbbbbb
..bbbbbwwbbbbbbwwwwwwwwbbbbbbb
..bbbbbbbbbbbbbbbbbbbbbbbbbbb.
..bbbbbbbbbbbbbbbbbbbbbbbbbbb.
..aaaaaaaaaaaaaaaaaaaaaaaaab..
...aaaaaaaaaaaaaaaaaaaaaaa....
...aaaaaaaaaaaaaaaaaaaaa......
....aaaaaaaaaaaaaaaaaaaa......
.....aaaaaaaaaaaaaaaaaa.......
.......aaaaaaaaaaaaaaaa.......
............aaaaaaaaaa........
..............aaaaaa..........
"""),
    Logo(
        'github', 'GitHub 章鱼猫', ('octocat', 'gh', 'git', '章鱼猫'),
        palette={'o': ('#24292F', 0), 'w': ('#F0F6FC', 1)},
        art="""
............oooooooo............
.........oooooooooooooo.........
.......oooooooooooooooooo.......
......oooooooooooooooooooo......
.....oooooooooooooooooooooo.....
....oooooooooooooooooooooooo....
...oooooooooooooooooooooooooo...
..ooooowwwwoooooooooowwwwooooo..
..ooooowwwwwwwwwwwwwwwwwwooooo..
.oooooowwwwwwwwwwwwwwwwwwoooooo.
.oooooowwwwwwwwwwwwwwwwwwoooooo.
.oooooowwwwwwwwwwwwwwwwwwoooooo.
oooooowwwwwwwwwwwwwwwwwwwwoooooo
oooooowwwwwwwwwwwwwwwwwwwwoooooo
oooooowwwwwwwwwwwwwwwwwwwwoooooo
oooooowwwwwwwwwwwwwwwwwwwwoooooo
oooooowwwwwwwwwwwwwwwwwwwwoooooo
oooooowwwwwwwwwwwwwwwwwwwwoooooo
oooooowwwwwwwwwwwwwwwwwwwwoooooo
ooooooowwwwwwwwwwwwwwwwwwooooooo
.oooooowwwwwwwwwwwwwwwwwwoooooo.
.ooooooowwwwwwwwwwwwwwwwooooooo.
.oooowoooowwwwwwwwwwwwooooooooo.
..ooowwoooooowwwwwwooooooooooo..
..oooowwoooowwwwwwwwoooooooooo..
...oooowwoowwwwwwwwwooooooooo...
....ooowwwwwwwwwwwwwoooooooo....
.....ooowwwwwwwwwwwwooooooo.....
......oooooowwwwwwwwoooooo......
.......ooooowwwwwwwwooooo.......
.........ooowwwwwwwwooo.........
............wwwwwwww............
"""),
    Logo(
        'gemini', 'Gemini 星星', ('google', 'google-gemini', 'bard', '星星'),
        palette={'b': ('#3186FF', 0), 'r': ('#F84040', 0), 'y': ('#F8B810', 0), 'g': ('#08B860', 0), 'c': ('#2098C0', 0), 'p': ('#5B8CF0', 0)},
        art="""
...............rr...............
...............rr...............
...............rr...............
..............rrrr..............
..............rrrr..............
.............rrrrrr.............
.............rrrrrp.............
............rrrrrppp............
...........rrrrpppppp...........
..........rrrppppppppp..........
.........rrrppppppppbbb.........
........rrrppppppppbbbbb........
.......yrrpppppppbbbbbbbb.......
.....yyyyrpppppbbbbbbbbbbbb.....
...yyyyyyrppppbbbbbbbbbbbbbbb...
yyyyyyyyyycccbbbbbbbbbbbbbbbbbbb
yyyyyyyyygcccbbbbbbbbbbbbbbbbbbb
...yyyyyygccccbbbbbbbbbbbbbbb...
.....yyyyggccccbbbbbbbbbbbb.....
.......yyggcccccbbbbbbbbb.......
........ygggcccccbbbbbbb........
.........ggggccccbbbbbb.........
..........gggcccccbbbb..........
...........gggcccccbb...........
............gggccccc............
.............gggccc.............
.............gggccc.............
..............gggc..............
..............gggg..............
...............gg...............
...............gg...............
...............gg...............
"""),
    Logo(
        'doubao', '豆包', ('bytedance', 'doubao-girl', '豆包'),
        palette={'b': ('#CFE3FA', 0), 'h': ('#4A3631', 2), 'H': ('#6E5148', 2), 'd': ('#2C201D', 2), 'f': ('#F4DCD3', 1), 'F': ('#E8C2B6', 1), 'G': ('#D8A99D', 1), 'k': ('#231A19', 1), 'K': ('#5C3A30', 1), 'w': ('#FFFFFF', 1), 'p': ('#EFA8A0', 1), 'm': ('#DC8276', 1), 'n': ('#C98878', 1), 't': ('#1E1D22', 1)},
        back_map={'f': 'h', 'F': 'h', 'G': 'h', 'k': 'h', 'K': 'h', 'w': 'h', 'p': 'h', 'm': 'h', 'n': 'h', 'H': 'h', 'd': 'h'},
        art="""
...................hhhhhhh....................
.................hhhhhhhhhd...................
...............hhhhhhhhhhhddHhH...............
..............Hhhhhhhhhhhhdddhhh..............
.............HHhhhhhhhhhhhhddhhhhh............
...........bGGhhhhhhhhhhhhhddhhhhdd...........
.........bbhGhhhhhhKKKKhhhhddhhhhdddb.........
........bbbhhhhhhhHHHhhhhhhhhhhhhhddbb........
.......bbbhHhhhhhKHHhhdddddhhHKhhhddhbb.......
......bbbbhhhhhhhHKhhdddGffffhhhhhddhbbb......
.....bbbbhGhhhhhKKhddhGFffffffhhhhhddbbbb.....
....bbbbbhHhhhhKKhdhhGFfffffffffhhhhdbbbbb....
....bbbbbhHhhhhKhdhHGFffffffffffFhhhhdbbbb....
...bbbbbhHhhhhhhdhKGFfffffffffffFFdhhdbbbbb...
...bbbbbhHhhhhhddKGGFfffffffffffFFFddhbbbbb...
..bbbbbbhHhhhhhdhKdGFfffffffffFFFFFddhbbbbbb..
..bbbbbhHKhhhhddkkdhKKfffffffFFKFFFFdhbbbbbb..
.bbbbbbhHhhhhhdhGGGFFFFfffffFFKKhhKGdhhbbbbbb.
.bbbbbbhHhhhhhdHGGGFFFFffffFFFFFFFFGGdbbbbbbb.
.bbbbbbHHhhhhhdkkkkkFFFffffFFFFFFFFGGdbbbbbbb.
bbbbbbbHHhhhhhdkHkkdddFFfffFFFkkkdFFGdbbbbbbbb
bbbbbbbHHhhhhdKGGkkddFFFfffFFddkkhdFGdbbbbbbbb
bbbbbbbHHhhhhdKGGkkkKffffffFFhwkkbhFGGGbbbbbbb
bbbbbbbHHhhhhhKGGkhhfffffffFFhkkKFfFGFFFbbbbbb
bbbbbbbHHhhhhhKGGpFFFfffffFffFhhpFFFGFFFbbbbbb
bbbbbbbHHhhhhhKGGpFFffffffFffFFppFFGFFFFbbbbbb
bbbbbbbhHhhhhhKGpFFfffffFGpffFFFFFFGFFFbbbbbbb
bbbbbbbHHhhhhhHGGFFFfffffffffFFFFFGGFFbbbbbbbb
bbbbbbbHHhhhhhdGGFFFfffffffffFFFFGhbbbbbbbbbbb
bbbbbbbhHhhhhhdGGGFFFfFppmmpFFFFGGdbbbbbbbbbbb
.bbbbbbbHhhhhKddGGGFFffmmmmFFFFGGkdbbbbbbbbbb.
.bbbbbbbHhhhhKdddhnGFffffffFFGGdkkdbbbbbbbbbb.
.bbbbbbbHhhhhKddddhnGFfffffFGGddkkdbbbbbbbbbb.
..bbbbbbbbbhhhdbbhhhnGGGFFGhdddddkdbbbbbbbbb..
..bbbbbbbbbbbbbbbbbbnnnnnnnbddddddbbbbbbbbbb..
...bbbbbbbbbbbbbbbbnnnnnnnbbbbbbbbbbbbbbbbb...
...bbbbbbbbbbbbbbbbnGGGGGnnbbbbbbbbbbbbbbbb...
....bbbbbbbbbbbbbbbnGpppGGnbbbbbbbbbbbbbbb....
....bbbbbbbbbbbbbbnGpFFFFpGnbbbbbbbbbbbbbb....
.....bbbbbbbbbbbbnGFFFFFFFFGFkttdtbbbbbbb.....
......bbbbbbbbtkkkfFFfffFFFffkkkttthbbbb......
.......bbbbbttttttkfffffffffttttttttbbb.......
........bbhtttttttttttdffttttttttttttb........
.........httttttttttttttttttttttttttt.........
...........tttttdtttttttttttttttttt...........
.............tttttttttttttttttttt.............
...............tttttttttttttttt...............
..................dttttttttt..................
"""),
    Logo(
        'deepseek', 'DeepSeek 鲸鱼', ('ds', 'whale', '深度求索', '鲸鱼'),
        palette={'b': ('#4D6BFE', 0)},
        art="""
...............bbb.....b..........
.......bbbbbbbbbb......bb.........
....bbbbbbbbbbbbb......bbbb.....bb
...bbbbbbbbbbbbbbb.....bbbbb.bbbbb
..bbbbbbbbbbbbbbbbb....bbbbbbbbbbb
..bbbbbbbbbbbbbbbbbb...bbbbbbbbbb.
.bbbbbbbbbbbbbbbbbbbb...bbbbbbbb..
.bbbbbbbbbbbbbbbbbbbbb...bbbbbb...
bbb..bbbbbbbbbbbbbbbbbb..bbbb.....
bb.......bbbbbbbbb.bbbbbbbbbb.....
bb........bbbbbbbb...bbbbbbb......
bbb.........bbbbbb....bbbbbb......
bbb..........bbbbbb...bbbbbb......
bbb...........bbbbbb..bbbbbb......
bbb............bbbbbbbbbbbb.......
.bbb...........bbbbbbbbbbbb.......
.bbbb...........bbbbbbbbbb........
..bbb............bbbbbbbb.........
..bbbb.....bb....bbbbbbbb.........
...bbbb....bbbb...bbbbbb..........
....bbbbb...bbbb...bbbbbbb........
.....bbbbbbbbbbbbb..bbbbbbb.......
......bbbbbbbbbbbbbb..............
........bbbbbbbbbbb...............
"""),
    Logo(
        'baidu', '百度 熊掌', ('du', 'paw', '百度', '熊掌'),
        palette={'b': ('#2932E1', 0), 'w': ('#FFFFFF', 1)},
        art="""
.........bbb................
........bbbbb....bbbb.......
........bbbbb....bbbbb......
.......bbbbbbb..bbbbbb......
.......bbbbbbb..bbbbbb......
.......bbbbbbb.bbbbbbb......
........bbbbb..bbbbbbb......
..bbb...bbbbb...bbbbbb......
.bbbbb...bbb....bbbbb.......
.bbbbbb...........b...bbbb..
.bbbbbb..............bbbbbb.
bbbbbbb..............bbbbbb.
bbbbbbb.....bbb......bbbbbbb
.bbbbbb....bbbbbb....bbbbbbb
.bbbbb....bbbbbbbb...bbbbbbb
..bbb....bbbbbbbbbb..bbbbbb.
........bbbbbbbbbbb..bbbbbb.
.......bbbbwwbbbbbbb...bbb..
......bbbbbwwbbbbbbbb.......
.....bbbbbbwwbbbbbbbbb......
....bbbwwwwwwbwwbbbwwbb.....
...bbbwwwbbwwbwwbbbwwbbb....
...bbbwwbbbwwbwwbbbwwbbbb...
..bbbbwwbbbwwbwwbbbwwbbbb...
..bbbbwwbbbwwbwwbbbwwbbbbb..
..bbbbwwwbbwwbwwwwwwwbbbbb..
...bbbbwwwwwwbbwwwwwwbbbb...
...bbbbbbbbbbbbbbbbbbbbbb...
....bbbbbbbbbbbbbbbbbbbb....
......bbbb.......bbbbbb.....
"""),
    Logo(
        'vscode', 'VS Code', ('code', 'vsc', 'visualstudiocode', 'visual-studio-code'),
        palette={'l': ('#24A8F0', 0), 'm': ('#0088D0', 0), 'd': ('#0068B0', 0)},
        art="""
.....................dl.......
....................ddlll.....
...................mddlllll...
.................dddddlllllll.
................ddddddllllllll
...............dddddddllllllll
..............dddddddddlllllll
..mm.........ddddddddddlllllll
.mmmm.......ddddddddddllllllll
mmmmmm.....dddddddddd..lllllll
mmmmmmmm..ddddddddd....lllllll
.mmmmmmmmddddddddd.....lllllll
..mmmmmmmmddddddd......lllllll
...mmmmmmmmmddd........lllllll
....mmmmmmmmmd.........lllllll
....dmmmmmmmmm.........lllllll
...dddmmmmmmmmm........lllllll
..ddddddmmmmmmmmm......lllllll
.ddddddddmmmmmmmmm.....lllllll
dddddddd..mmmmmmmmm....lllllll
dddddd.....mmmmmmmmmm..lllllll
.dddd.......mmmmmmmmmmllllllll
..dd.........mmmmmmmmmllllllll
..............mmmmmmmmllllllll
...............mmmmmmmllllllll
................mmmmmmdlllllll
.................mmmmmlllllll.
...................mmmlllll...
....................mmlll.....
.....................ml.......
"""),
    Logo(
        'apple', 'Apple 苹果', ('mac', 'macos', 'ios', '苹果'),
        palette={'a': ('#EDEEF0', 0), 'b': ('#D5D8DB', 0), 'c': ('#BCC1C5', 0), 'd': ('#A2AAAD', 0)},
        art="""
.................a.......
...............aaa.......
..............aaaa.......
.............aaaaa.......
.............aaaa........
.............aaa.........
............aaa..........
......aaa.......aaaa.....
....bbbbbbbb.bbbbbbbbb...
...bbbbbbbbbbbbbbbbbbbb..
..bbbbbbbbbbbbbbbbbbbbbb.
.bbbbbbbbbbbbbbbbbbbbb...
.bbbbbbbbbbbbbbbbbbbbb...
.bbbbbbbbbbbbbbbbbbbb....
bbbbbbbbbbbbbbbbbbbbb....
ccccccccccccccccccccc....
ccccccccccccccccccccc....
ccccccccccccccccccccc....
ccccccccccccccccccccc....
.ccccccccccccccccccccc...
.cccccccccccccccccccccc..
.ccccccccccccccccccccccc.
..ccccccccccccccccccccccc
..dddddddddddddddddddddd.
..dddddddddddddddddddddd.
...dddddddddddddddddddd..
....dddddddddddddddddd...
....dddddddddddddddddd...
.....dddddddddddddddd....
......dddd......dddd.....
"""),
    Logo(
        'netease', '网易云音乐', ('ncm', '163', 'cloudmusic', 'neteasecloudmusic', '网易云'),
        palette={'o': ('#E60026', 0), 'w': ('#FFFFFF', 1)},
        art="""
............oooooooo............
.........oooooooooooooo.........
.......oooooooooooooooooo.......
......oooooooooooooooooooo......
.....oooooooooooooooooooooo.....
....oooooooooooooooooooooooo....
...ooooooooooooooowwwwooooooo...
..ooooooooooooooowwwwwoooooooo..
..ooooooooowwwooowwooooooooooo..
.ooooooooowwwoooowwoooooooooooo.
.oooooooowwwoooowwwwooooooooooo.
.ooooooowwwooowwwwwwwwooooooooo.
ooooooowwwooowwwwwwwwwwooooooooo
ooooooowwoooowwooowwowwwoooooooo
ooooooowwooowwwooowwoowwwooooooo
ooooooowwooowwoooowwooowwooooooo
ooooooowwooowwoooowwooowwooooooo
ooooooowwooowwwooowwooowwooooooo
ooooooowwoooowwwwwwwooowwooooooo
ooooooowwwoooowwwwwoooowwooooooo
.ooooooowwoooooooooooowwwoooooo.
.ooooooowwwooooooooooowwooooooo.
.oooooooowwwooooooooowwwooooooo.
..oooooooowwwwoooowwwwwooooooo..
..ooooooooowwwwwwwwwwooooooooo..
...oooooooooowwwwwwoooooooooo...
....oooooooooooooooooooooooo....
.....oooooooooooooooooooooo.....
......oooooooooooooooooooo......
.......oooooooooooooooooo.......
.........oooooooooooooo.........
............oooooooo............
"""),
    Logo(
        'wechat', '微信', ('weixin', 'wx'),
        palette={'g': ('#07C160', 0), 'w': ('#FFFFFF', 1)},
        art="""
....gggggggggggggggggggggg....
..gggggggggggggggggggggggggg..
.gggggggggggggggggggggggggggg.
.gggggggggggggggggggggggggggg.
gggggggggggggggggggggggggggggg
gggggggggggggggggggggggggggggg
gggggggggwwwwwwggggggggggggggg
gggggggwwwwwwwwwwggggggggggggg
ggggggwwwwwwwwwwwwgggggggggggg
gggggwwwwwwwwwgwwwwggggggggggg
ggggwwwwggwwwwggwwwggggggggggg
ggggwwwwwgwwwwgwwwwwgggggggggg
ggggwwwwwwwwwwwwwwwwwwgggggggg
ggggwwwwwwwwwwwwwwwwwwwwgggggg
ggggwwwwwwwwwwwwwwwwwwwwwggggg
gggggwwwwwwwwwwwwgwwwgwwwwgggg
gggggwwwwwwwwwwwwgwwwgwwwwgggg
ggggggwwwwwwwwwwwwwwwwwwwwgggg
gggggggwwwwwwwwwwwwwwwwwwwgggg
gggggggwgggggwwwwwwwwwwwwwgggg
ggggggggggggggwwwwwwwwwwwggggg
ggggggggggggggwwwwwwwwwwgggggg
ggggggggggggggggwwwwwwwwgggggg
gggggggggggggggggggggggwgggggg
gggggggggggggggggggggggggggggg
gggggggggggggggggggggggggggggg
.gggggggggggggggggggggggggggg.
.gggggggggggggggggggggggggggg.
..gggggggggggggggggggggggggg..
....gggggggggggggggggggggg....
"""),
    Logo(
        'intel', 'Intel 英特尔', ('英特尔',),
        palette={'b': ('#0071C5', 0)},
        depth=3,
        art="""
bb................................bbb
bbb.............bbb...............bbb
bbb.............bbb...............bbb
................bbb...............bbb
................bbb...............bbb
bbb..bb.bbbbb...bbbbb....bbbbb....bbb
bbb.bbbbbbbbbb..bbbbb..bbbbbbbb...bbb
bbb.bbbbb..bbb..bbbb...bbb..bbbb..bbb
bbb.bbbb....bbb.bbb...bbb.....bb..bbb
bbb.bbb.....bbb.bbb...bbbbbbbbbbb.bbb
bbb.bbb.....bbb.bbb...bbbbbbbbbbb.bbb
bbb.bbb.....bbb.bbb...bbb.........bbb
bbb.bbb.....bbb.bbb...bbb.........bbb
bbb.bbb.....bbb..bbbb..bbbbbbbbb..bbb
bbb.bbb.....bbb..bbbb...bbbbbbb...bbb
.b...bb.....bb.....bb.....bbb.....bb.
"""),
    Logo(
        'amd', 'AMD', ('超威',),
        palette={'w': ('#F2F2F2', 0)},
        depth=3,
        art="""
..................................wwwwwwwwww
...wwww....www.....ww..wwwwwww.....wwwwwwwww
...wwww....wwww...www..wwwwwwww.....wwwwwwww
...wwww....wwwww.wwww..ww....www.........www
..www.ww...wwwwwwwwww..ww....www.........www
..ww..ww...www.www.ww..ww.....ww...w.....www
.www..www..www.ww..ww..ww....www..ww.....www
.wwwwwwww..www.....ww..ww....www..www....www
.ww....www.www.....ww..wwwwwwww...wwwwww..ww
www.....ww.www.....ww..wwwwwww....wwwww....w
..................................wwww......
"""),
    Logo(
        'nvidia', 'NVIDIA 英伟达', ('nv', 'geforce', '英伟达'),
        palette={'g': ('#76B900', 0)},
        depth=4,
        art="""
.............ggggggggggggggggggggggg
.............ggggggggggggggggggggggg
.............ggggggggggggggggggggggg
............g....ggggggggggggggggggg
.........gggg.......gggggggggggggggg
......ggggg..gggg.....gggggggggggggg
.....ggg.....ggggggg....gggggggggggg
...gggg.....g....gggg....ggggggggggg
..ggg....gggg......ggg....gggggggggg
.ggg....ggg..gg.....ggg....ggggggggg
gggg...ggg...ggg....ggg....ggggggggg
.ggg...ggg...gggg..gggg...gggggggggg
.gggg..ggg...ggggggggg...ggggggggggg
..ggg...ggg..gggggggg...gggg..gggggg
..gggg...ggg.gggggg....gggg.....gggg
...gggg...ggggggg....ggggg.......ggg
....gggg...gg......ggggg.......ggggg
.....gggg.....gggggggg........gggggg
......ggggg..ggggggg.......ggggggggg
........ggggg...........gggggggggggg
..........ggg.......gggggggggggggggg
.............ggggggggggggggggggggggg
.............ggggggggggggggggggggggg
.............ggggggggggggggggggggggg
"""),
    Logo(
        'c', 'C 语言', ('clang', 'c-lang', 'c语言'),
        palette={'l': ('#659AD2', 0), 'd': ('#00599C', 0), 'D': ('#004482', 0), 'w': ('#FFFFFF', 1)},
        art="""
...............lll...............
.............lllllll.............
...........llllllllll............
..........lllllllllllll..........
........lllllllllllllllll........
......lllllllllllllllllllll......
....lllllllllwwwwwwwlllllllll....
...llllllllwwwwwwwwwwwllllllll...
.llllllllwwwwwwwwwwwwwwwllllllll.
llllllllwwwwwwwwwwwwwwwwwlllllldd
lllllllwwwwwwwwwwwwwwwwwwwllldddd
llllllwwwwwwwwwwwwwwwwwwwwwdddddd
llllllwwwwwwwwlllllwwwwwwwddddddd
lllllwwwwwwwwlllllllwwwwddddddddd
lllllwwwwwwwlllllllllwddddddddddd
lllllwwwwwwlllllllllddddddddddddd
lllllwwwwwwlllllllldddddddddddddd
lllllwwwwwwlllllldddddddddddddddd
lllllwwwwwwllllDDdddddddddddddddd
lllllwwwwwwllDDDDDDdddddddddddddd
lllllwwwwwwlDDDDDDDDDdddddddddddd
lllllwwwwwwwDDDDDDDDDwwdddddddddd
lllllwwwwwwwwDDDDDDDwwwwddddddddd
llllllwwwwwwwwDDDDDwwwwwwwddddddd
lllllDwwwwwwwwwwwwwwwwwwwwwDddddd
lllDDDDwwwwwwwwwwwwwwwwwwwDDDDddd
lDDDDDDDwwwwwwwwwwwwwwwwwDDDDDDDd
.DDDDDDDDwwwwwwwwwwwwwwwDDDDDDDD.
...DDDDDDDDwwwwwwwwwwwDDDDDDDD...
....DDDDDDDDDwwwwwwwDDDDDDDDD....
......DDDDDDDDDDDDDDDDDDDDD......
........DDDDDDDDDDDDDDDDD........
..........DDDDDDDDDDDDD..........
............DDDDDDDDD............
.............DDDDDDD.............
...............DDD...............
"""),
    Logo(
        'cpp', 'C++', ('c++', 'cplusplus', 'cxx'),
        palette={'l': ('#659AD2', 0), 'd': ('#00599C', 0), 'D': ('#004482', 0), 'w': ('#FFFFFF', 1)},
        art="""
..............lll..............
............lllllll............
..........lllllllllll..........
.........lllllllllllll.........
.......lllllllllllllllll.......
.....lllllllllllllllllllll.....
...lllllllllllllllllllllllll...
..llllllllllllwwwllllllllllll..
lllllllllllwwwwwwwwwlllllllllll
lllllllllwwwwwwwwwwwwwlllllllld
llllllllwwwwwwwwwwwwwwwlllllldd
lllllllwwwwwwwwwwwwwwwwwllldddd
llllllwwwwwwwwwwwwwwwwwwwdddddd
llllllwwwwwwwlllllwwwwwdddddddd
lllllwwwwwwwlllllllwwwddddddddd
lllllwwwwwwlllllllllddddddddddd
lllllwwwwwllllllllddddddwdddwdd
lllllwwwwwlllllldddddddwwwdwwwd
lllllwwwwwlllllDddddddddwdddwdd
lllllwwwwwlllDDDDDddddddddddddd
lllllwwwwwwDDDDDDDDDddddddddddd
lllllwwwwwwwDDDDDDDDwwddddddddd
llllllwwwwwwwDDDDDwwwwwdddddddd
llllllwwwwwwwwwwwwwwwwwwwdddddd
llllDDDwwwwwwwwwwwwwwwwwDDDdddd
lllDDDDDwwwwwwwwwwwwwwwDDDDDDdd
lDDDDDDDDwwwwwwwwwwwwwDDDDDDDDd
DDDDDDDDDDDwwwwwwwwwDDDDDDDDDDD
..DDDDDDDDDDDDwwwDDDDDDDDDDDD..
...DDDDDDDDDDDDDDDDDDDDDDDDD...
.....DDDDDDDDDDDDDDDDDDDDD.....
.......DDDDDDDDDDDDDDDDD.......
.........DDDDDDDDDDDDD.........
..........DDDDDDDDDDD..........
............DDDDDDD............
..............DDD..............
"""),
    Logo(
        'python', 'Python', ('py',),
        palette={'b': ('#3776AB', 0), 'B': ('#30669A', 0), 'y': ('#FFD43B', 0), 'w': ('#F8F8F8', 0)},
        art="""
..........bbbbbbbb..........
........bbbbbbbbbbbb........
.......bbwwbbbbbbbbb........
.......bbwwbbbbbbbbbb.......
.......bbbwbbbbbbbbbB.......
.......bbbbbbbbbbbbBB.......
..............bbbbBBB.......
...bbbbbbbbbbbbbbBBBB.yyy...
..bbbbbbbbbbbbbbBBBBB.yyyyy.
.bbbbbbbbbbbbbbBBBBBB.yyyyy.
.bbbbbbbbbbbbbbBBBBBByyyyyy.
bbbbbbbbbbbbbbBBBBBBwyyyyyyy
bbbbbbbbbbbbbBBBBBBBwyyyyyyy
bbbbbbbbbbbbBBBBBBwwyyyyyyyy
bbbbbbbbwwyyyyyyyyyyyyyyyyyy
bbbbbbbwyyyyyyyyyyyyyyyyyyyy
bbbbbbbwyyyyyyyyyyyyyyyyyyyy
.bbbbb.yyyyyyyyyyyyyyyyyyyy.
.bbbbb.yyyyyyyyyyyyyyyyyyyy.
..bbbb.yyyyyyyyyyyyyyyyyyy..
...bbb.yyyyyyyyyyyyyyyyyy...
.......yyyyyyy..............
.......yyyyyyyyyyyyyy.......
.......yyyyyyyyyywyyy.......
.......yyyyyyyyyywwyy.......
........yyyyyyyyywwy........
.........yyyyyyyyyy.........
..........yyyyyyyy..........
"""),
    Logo(
        'js', 'JavaScript', ('javascript', 'ecmascript', 'node'),
        palette={'y': ('#F7DF1E', 0), 'k': ('#323330', 1)},
        art="""
yyyyyyyyyyyyyyyyyyyyyyyyyy
yyyyyyyyyyyyyyyyyyyyyyyyyy
yyyyyyyyyyyyyyyyyyyyyyyyyy
yyyyyyyyyyyyyyyyyyyyyyyyyy
yyyyyyyyyyyyyyyyyyyyyyyyyy
yyyyyyyyyyyyyyyyyyyyyyyyyy
yyyyyyyyyyyyyyyyyyyyyyyyyy
yyyyyyyyyyyyyyyyyyyyyyyyyy
yyyyyyyyyyyyyyyyyyyyyyyyyy
yyyyyyyyyyyyyyyyyyyyyyyyyy
yyyyyyyyyyyyyyyyyyyyyyyyyy
yyyyyyyyyyyyyyyyyyyyyyyyyy
yyyyyyyyyyyykkyyykkkkkyyyy
yyyyyyyyyyyykkyyykkkkkkyyy
yyyyyyyyyyyykkyykkkyykyyyy
yyyyyyyyyyyykkyykkkyyyyyyy
yyyyyyyyyyyykkyykkkkyyyyyy
yyyyyyyyyyyykkyyykkkkkyyyy
yyyyyyyyyyyykkyyyyykkkkyyy
yyyyyyyyyyyykkyyyyyyykkkyy
yyyyyyyyyyyykkyyyyyyykkkyy
yyyyyyykkkykkkyykkkyykkkyy
yyyyyyykkkkkkkyykkkkkkkyyy
yyyyyyyyykkkyyyyyykkkkyyyy
yyyyyyyyyyyyyyyyyyyyyyyyyy
yyyyyyyyyyyyyyyyyyyyyyyyyy
"""),
    Logo(
        'ubuntu', 'Ubuntu', ('乌班图',),
        palette={'o': ('#E95420', 0), 'w': ('#FFFFFF', 1)},
        art="""
...........oooooooo...........
........oooooooooooooo........
.......oooooooooooooooo.......
.....oooooooooooooooooooo.....
....oooooooooooooooooooooo....
...ooooooooowwwwoowwwwooooo...
...ooooooowwwwwwowwwwwwoooo...
..ooooooowwwwwwoowwwwwwooooo..
.ooooooowwwwooooowwwwwwoooooo.
.oooooowwwwooooooowwwwooooooo.
.oooooooooooooooooowwoowwoooo.
oooooowwooooooooooooooowwooooo
ooooowwwwwooooooooooooowwooooo
oooowwwwwwooooooooooooowwwoooo
oooowwwwwwooooooooooooowwwoooo
oooowwwwwwooooooooooooowwwoooo
ooooowwwwoooooooooooooowwwoooo
ooooooooooooooooooooooowwooooo
oooooooooooooooooooooowwwooooo
.oooooowwwoooooooowwwoowwoooo.
.oooooowwwwoooooowwwwwowooooo.
.ooooooowwwwoooowwwwwwooooooo.
..ooooooowwwwwwowwwwwwoooooo..
...oooooooowwwwoowwwwwooooo...
...oooooooooowwwowwwwoooooo...
....oooooooooooooooooooooo....
.....oooooooooooooooooooo.....
.......oooooooooooooooo.......
........oooooooooooooo........
...........oooooooo...........
"""),
    Logo(
        'linux', 'Linux 企鹅', ('tux', 'penguin', '企鹅'),
        palette={'k': ('#1C1C1C', 0), 'w': ('#F8F8F8', 0), 'y': ('#F8C800', 0), 'o': ('#E08800', 0), 'g': ('#989898', 0), 'n': ('#985800', 0)},
        art="""
.............kkkk.............
...........kkkkkkkk...........
...........kkkkkkkkk..........
..........kkkkkkkkkk..........
..........kkkkkkkkkkk.........
..........kkkkkwwkkkk.........
..........kwwkkwkwkkk.........
..........kkkkkkkwkkk.........
..........kkyyyyywkkk.........
..........kyyyyyyykkk.........
..........kyyyyyyykkk.........
..........kgyyyygwkkgk........
..........kwgggwwwwkkk........
.........kkwwgwwwwwkkkk.......
.........kwwwwwwwwwwkkkk......
........kkwwwwwwwwwwkkkk......
.......kkkwwwwwwwwggkkkkk.....
.......kkwwwwwwwwwwwgkkkkk....
......kkwwwwwwwwwwwwwkkkkk....
......kkwwwwwwwwwwwwwwkkkkk...
......kkwwwwwwwwwwwwwwkkkkk...
.....kkwwwwwwwwwwwwwwwkkkkk...
.....kkwwwwwwwwwwwwwwwkkkkk...
....kkkwwwwwwwwwwwwwwwkkkkk...
....yyywwwwwwwwwwwwwwwkkkkk...
....yyyykwwwwwwwwwwwyykkkky...
..yyyyyykkwwwwwwwwwwyykkkyy...
yyyyyyyyykkwwwwwwwwwyyynyyyy..
yyyyyyyyykkkwwwwwwwwnyyyyyyyy.
yyyyyyyyyykwwwwwwwwknyyyyyyyyy
yyyyyyyyyyywwwwwwwkkyyyyyyyyyy
yyyyyyyyyyykwwwkkkkkyyyyyyyy..
yyyyyyyyyyykkkkkkkkkyyyyyyy...
...nnnyyyyokkkkk.kkkoyyyn.....
......nnnnn.........nnnn......
"""),
    Logo(
        'redhat', 'Red Hat 红帽', ('rhel', 'fedora-hat', '红帽'),
        palette={'r': ('#EE0000', 0), 'k': ('#1A1A1A', 0)},
        art="""
................rrr...............
..........rrrrrrrrrrrr............
.........rrrrrrrrrrrrrrr..........
.........rrrrrrrrrrrrrrrrr........
.........rrrrrrrrrrrrrrrrr........
........rrrrrrrrrrrrrrrrrrr.......
........rrrrrrrrrrrrrrrrrrr.......
........rrrrrrrrrrrrrrrrrrr.......
........rrrrrrrrrrrrrrrrrrr.......
....rrr.rrrrrrrrrrrrrrrrrrrr......
.rrrrrr.kkrrrrrrrrrrrrrrrrrr......
rrrrrrr.kkkrrrrrrrrrrrrrrrrr......
rrrrrrr.kkkkkrrrrrrrrrrrrrrr......
rrrrrrrrkkkkkkkrrrrrrrrrrrrr.r....
rrrrrrrrrkkkkkkkkkrrrrrrrrkk.rr...
.rrrrrrrrrrkkkkkkkkkkkkkkkkk.rrr..
.rrrrrrrrrrrrkkkkkkkkkkkkkkkrrrrr.
..rrrrrrrrrrrrrkkkkkkkkkkkkrrrrrrr
...rrrrrrrrrrrrrrrrrkkkkrrrrrrrrrr
....rrrrrrrrrrrrrrrrrrrrrrrrrrrrrr
......rrrrrrrrrrrrrrrrrrrrrrrrrrrr
........rrrrrrrrrrrrrrrrrrrrrrrrrr
..........rrrrrrrrrrrrrrrrrrrrrrr.
............rrrrrrrrrrrrrrrrrrrr..
...............rrrrrrrrrrrrrrrr...
....................rrrrrrrr......
"""),
    Logo(
        'windows', 'Windows', ('win', 'win11', 'microsoft', 'ms'),
        palette={'a': ('#30B0F0', 0), 'b': ('#1A98E8', 0), 'c': ('#0A82DA', 0), 'd': ('#006CC4', 0)},
        art="""
aaaaaaaaaaa..bbbbbbbbbbc
aaaaaaaaaaa..bbbbbbbbbcc
aaaaaaaaaab..bbbbbbbbccc
aaaaaaaaabb..bbbbbbbcccc
aaaaaaaabbb..bbbbbbccccc
aaaaaaabbbb..bbbbbcccccc
aaaaaabbbbb..bbbbccccccc
aaaaabbbbbb..bbbcccccccc
aaaabbbbbbb..bbccccccccc
aaabbbbbbbb..bcccccccccc
aabbbbbbbbb..ccccccccccc
........................
........................
bbbbbbbbbbc..cccccccccdd
bbbbbbbbbcc..ccccccccddd
bbbbbbbbccc..cccccccdddd
bbbbbbbcccc..ccccccddddd
bbbbbbccccc..cccccdddddd
bbbbbcccccc..ccccddddddd
bbbbccccccc..cccdddddddd
bbbcccccccc..ccddddddddd
bbccccccccc..cdddddddddd
bcccccccccc..ddddddddddd
ccccccccccc..ddddddddddd
"""),
    Logo(
        'vim', 'Vim', ('vi', 'neovim', 'nvim'),
        palette={'g': ('#019833', 0), 'G': ('#007F10', 0), 'L': ('#40F800', 0), 'l': ('#C8C8C8', 1), 'm': ('#808080', 1), 'w': ('#F8F8F8', 1), 'k': ('#101010', 1)},
        art="""
.......................k.......................
......................lLk......................
...kkkkkkkkkkkkkkkkk.llLLk...kkkkkkkkkkkkkkkk..
..kkwwwwwwwwwwwwwwwwklggLLk.kwwwwwwwwwwwwwwwwk.
..kwlllllllllllllllmkggggLLkkwlllllllllllllllm.
..kwlllllllllllllllmkgggggLLkwlllllllllllllllm.
..kwlllllllllllllllmkggggggLkwllllllllllllllmm.
...kkkwlllllllllmmkkkgggggggkkkmlllllllllllmmk.
.....kwlllllllllmkkggggggggggkkwllllllllllmmk..
.....kwlllllllllmkkggggggggggkwwlllllllllmmk...
.....kwlllllllllmkkgggggggggkwwlllllllllmmk....
.....kwlllllllllmkkggggggggkwwlllllllllmmk.....
.....kwlllllllllmkkgggggggkwwlllllllllmmk......
.....kwlllllllllmkkggggggkwwlllllllllmmk.......
.....kwlllllllllmkkgggggkwwllllllllllmk........
.....kwlllllllllmkkggggkwwllllllllllmkk........
.....kwlllllllllmkkgggkwwllllllllllmkLLk.......
.....kwlllllllllmkkggkwwllllllllllmkggLLk......
.....kwlllllllllmkkgkwwllllllllllmkggggLLk.....
....lkwlllllllllmkkkwwllllllllllmkggggggLLk....
...llkwlllllllllmkkwwllllllllllmkggggggggLLk...
..llgkwlllllllllmmwwllllllllllmkggggggggggLLk..
.llggkwlllllllllmwwllllllllllmkggggggggggggLLk.
llgggkwlllllllllmwllllllllllmkggggggggggggggLLk
GGgggkwlllllllllmllllllllllmkgggggggggggggggGG.
.GGggkwlllllllllllllllllllmkkggggggggggggggGG..
..GGgkwlllllllllllllllllkkkkggggggggggggggGG...
...GGkwllllllllllllllllkllkggggggggggggggGG....
....GkwlllllllllllllllllllkgggggggggggggGG.....
.....kwlllllllllllllllklllgggggggggggggGG......
.....kwllllllllllllllmkkkgggggggggggggGG.......
.....kwlllllllllllllmkkggggggggggggggGG........
.....kwllllllllllllmkllllkgklllkggklkkkkkkkk...
.....kwlllllllllllmkkllllggkllllllllllklllll...
.....kwllllllllllmkkglllkggklllllllllllllllk...
.....kwlllllllllmkkgklllkggkllkkkklllkkklllk...
.....kwllllllllmkkggklllggglllkgGkllk..klll....
.....kwlllllllmkkggglllkggklllgGGlllk..klll....
.....kwllllllmkkgggklllkggkllkGGklll...lllk....
.....kwlllllmkkGGggklllggglllkG.kllk...lll.....
.....kwllllmmk..GGglllkggklllk..lllk..klll.....
.....kwlllmmk....Gkllllkgklllk..lllk..llllk....
......kkkkkk......kkkkkggkkkk...kkkk..kkkk.....
...................GGggggGG....................
....................GGggGG.....................
.....................GGGG......................
......................GG.......................
"""),
    Logo(
        'qt', 'Qt', ('qt5', 'qt6'),
        palette={'g': ('#41CD52', 0), 'k': ('#FFFFFF', 1)},
        art="""
....gggggggggggggggggggggggggggggg
...ggggggggggggggggggggggggggggggg
..gggggggggggggggggggggggggggggggg
.ggggggggggggggggggggggggggggggggg
gggggggggggkkkgggggggggggggggggggg
gggggggggkkkkkkkkgggggkkgggggggggg
ggggggggkkkkgkkkkgggggkkgggggggggg
gggggggkkkgggggkkkggggkkgggggggggg
gggggggkkkggggggkkggkkkkkkkggggggg
gggggggkkgggggggkkggkkkkkkkggggggg
gggggggkkgggggggkkkggkkkgggggggggg
gggggggkkgggggggkkkgggkkgggggggggg
gggggggkkgggggggkkkgggkkgggggggggg
gggggggkkgggggggkkkgggkkgggggggggg
gggggggkkgggggggkkggggkkgggggggggg
gggggggkkkggggggkkggggkkgggggggggg
gggggggkkkgggggkkkggggkkgggggggggg
ggggggggkkkkkkkkkgggggkkkkkggggggg
gggggggggkkkkkkkkgggggkkkkkggggggg
ggggggggggggkgkkkggggggggggggggggg
gggggggggggggggkkggggggggggggggggg
gggggggggggggggkkkggggggggggggggg.
gggggggggggggggggggggggggggggggg..
ggggggggggggggggggggggggggggggg...
gggggggggggggggggggggggggggggg....
"""),
    Logo(
        'matlab', 'MATLAB', ('mathworks', 'matlab-membrane'),
        palette={'b': ('#4098D8', 0), 'B': ('#2070B0', 0), 'o': ('#F88040', 0), 'r': ('#E06038', 0), 'R': ('#C04030', 0), 'd': ('#802820', 0)},
        art="""
...................roo........
...................rooo.......
..................roooo.......
.................brooooo......
.................rrooooo......
................brrooooo......
...............bbrooooooo.....
..............bbrrooooooo.....
............bBBrrrooooooo.....
..........BBBBBrrroooooooo....
.........BBBdddrroooooooor....
.........dddddrrroooooooor....
.......bbddddrrrroooooooorr...
.....bbbbbdddrrrroooooooorr...
..bbbbbbbbbdRrrroooooooorrr...
bbbbbbbbbbbRRrrroooooooorrrr..
bbbbbbbbbbbRrrrroooooooorrrr..
.bbbbbbbbbRRrrrroooooooorrrRR.
...bbbbbbRRRrrrooooooooorrrRR.
....bbbbRRRRrrroooooooor..rRR.
.........RRrrrrooooooo......RR
..........Rrrrroooooo........R
..........Rrrrrooooo..........
...........rrrooooo...........
...........rrroooo............
............rrooo.............
............rroo..............
.............oo...............
"""),
    Logo(
        'latex', 'LaTeX', ('tex', 'context'),
        palette={'k': ('#F2F2F2', 0)},
        depth=4,
        art="""
..kkkk......kk....kkkk...kkkkkkkkk..............kkkkk....kkkk...
...kk.......kk....kk.....kkk.....k...............kkk......kk....
...kk......k.kk...k......kkk......................kkk.....k.....
...kk......k.kk...k......kkk......k................kk....k......
...kk.........kk..k......kkk......k................kkk..........
...kk.....k...kk.........kkk....kkkkkkkkkkkkkk......kkk.k.......
...kk.....k....k.........kkk......kkk.......kkk......kkk........
...kk....kkkkkkkk........kkk.......kk.........k......kkk........
...kk....k.....kk........kkk.......kk.........k.......kkk.......
...kk...........kk.......kkk.......kk.........k......k.kk.......
...kk...k.......kk.......kkk.......kk...............k..kkk......
...kk..kkk...k.kkkkk.....kkk.......kk.....k.........k...kkk.....
...kk........k...........kkk.......kk.....k........k.....kk.....
...kk........k...........kkk.......kkkkkkkk.......k......kkk....
...kk.......kk...........kkk.......kk....kk.......k.......kkk...
...kk......kk............kkk.......kk.....k......kk.......kkk...
kkkkkkkkkkkkk.........kkkkkkkkk....kk.....k...kkkkkk.....kkkkkkk
...................................kk..........k................
...................................kk..........k................
...................................kk...........................
...................................kk.........k.................
...................................kk........kk.................
................................kkkkkkkkkkkkkkk.................
"""),

]


def _norm(s):
    s = s.strip().lower()
    for ch in " -_.":
        s = s.replace(ch, "")
    return s


_INDEX = {}
for _i, _logo in enumerate(LOGOS):
    for _name in (_logo.key, _logo.name, _logo.name.split(" ")[0], str(_i + 1)) + _logo.aliases:
        _INDEX.setdefault(_norm(_name), _logo)


def find_logo(name):
    """按 key / 中文名 / 别名 / 序号（从 1 开始）查找 logo，找不到返回 None。"""
    return _INDEX.get(_norm(name))


def suggest(name, n=3):
    """给出与输入最相近的几个 logo key。"""
    keys = list(_INDEX)
    hits = difflib.get_close_matches(_norm(name), keys, n=n * 3, cutoff=0.5)
    out = []
    for h in hits:
        k = _INDEX[h].key
        if k not in out:
            out.append(k)
    return out[:n]


def search(query):
    """列表界面的搜索：返回名字或别名里包含 query 的 logo（保持原顺序）。"""
    q = _norm(query)
    if not q:
        return list(LOGOS)
    res = []
    for i, logo in enumerate(LOGOS):
        names = (logo.key, logo.name, str(i + 1)) + logo.aliases
        if any(q in _norm(n) for n in names):
            res.append(logo)
    return res


# ======================================================================
# app.py
# ======================================================================
"""交互式终端 3D Logo 查看器：状态、动画、交互与界面。"""



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
    """列表里色块用的颜色：底板（浮雕高度 <= 0）上最常见的颜色。"""
    counts = {}
    for r in logo.rows:
        for ch in r:
            if ch != ".":
                counts[ch] = counts.get(ch, 0) + 1
    base = dict((c, n) for c, n in counts.items() if logo.palette[c][1] <= 0) or counts
    ch = max(base, key=base.get)
    return int(logo.palette[ch][0].lstrip("#"), 16)


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



if __name__ == "__main__":
    sys.exit(main())
