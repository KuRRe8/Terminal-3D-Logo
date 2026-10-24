# -*- coding: utf-8 -*-
"""把像素画挤出成 3D 体素模型。

模型由若干矩形面组成：
* 顶/底“盖面”（法线 ±z）：同一高度的像素合并成一个大矩形，逐像素查表取色；
* 侧壁（法线 ±x / ±y）：相邻像素高度不同处的竖直面，连续的边会合并成长条。

坐标系：x 向右，y 向上，z 指向观察者；模型中心在原点，一个体素边长为 1。
"""

import math

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
