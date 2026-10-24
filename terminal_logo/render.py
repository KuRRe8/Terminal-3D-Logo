# -*- coding: utf-8 -*-
"""纯 Python 软件光栅化渲染器。

每个矩形面按透视投影到屏幕上，逐扫描线求出覆盖范围，再用逆单应矩阵把屏幕像素
映射回面上的 (u, v) 坐标查表取色。深度缓冲存的是 1/深度（越大越近）。
输出是一维像素数组，颜色为 0xRRGGBB 整数；DEFAULT_BG 表示“终端默认背景色”。
"""

import math

from .model import NORMALS

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
