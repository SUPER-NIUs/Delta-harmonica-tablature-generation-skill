#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""简谱图片的八度点识别（低清截图也能用）。

为什么需要它：简谱里数字上/下方那个小点决定红底还是绿底，漏一个就错一个音，
而低清截图放大后反而更糊，肉眼在极限上。这个脚本用像素规律来定：
数字是 ~18px 高的窄字块，八度点是 4~5px 的小方块，位置只可能在
「数字行与相邻歌词行之间的那条窄缝」里 —— 缝里出现的黑块就是点，不会误判成歌词。

用法：
    python3 read_jianpu.py 简谱.png [-o 工作目录]

输出：
    控制台     每行的数字个数、检测到的点，以及「槽位序列」（^ 高音 / v 低音 / _ 无点）
    overlay.png 把检测结果画回原图（红圈=检测到的点，蓝线=数字中心，绿线=数字行上下界）
               —— 用这张图肉眼验伪：圈错位、漏圈，一眼能看出来

务必看一眼 overlay.png 再动手转写。脚本只是把「找点」这一步自动化，
谱面本身的音级还是要人来读。

注意：如果图里有别的高对比度小色块（装饰、水印、图标），可能混进来；
改 --xmax 把图右边缘的花纹排除掉即可。
"""
import argparse
import os
import sys
from collections import Counter, deque

try:
    from PIL import Image, ImageDraw
except ImportError:
    sys.stderr.write("ERROR: 需要 Pillow。pip install pillow\n")
    raise SystemExit(3)

TH = 140          # 二值化阈值：低于它算前景
MIN_ROW_INK = 6   # 一行至少这么多前景像素才算文字行


def load_binary(path):
    im = Image.open(path).convert("L")
    W, H = im.size
    px = list(im.getdata())
    fg = [px[i] < TH for i in range(W * H)]
    return im, W, H, px, fg


def components(W, H, fg, xmax):
    """连通域（4 邻接），返回 [(x0,y0,x1,y1,像素数)]。"""
    seen = bytearray(W * H)
    out = []
    for i in range(W * H):
        if not fg[i] or seen[i] or (i % W) >= xmax:
            continue
        q = deque([i]); seen[i] = 1
        x0 = x1 = i % W; y0 = y1 = i // W; n = 0
        while q:
            c = q.popleft(); n += 1
            x = c % W; y = c // W
            if x < x0: x0 = x
            if x > x1: x1 = x
            if y < y0: y0 = y
            if y > y1: y1 = y
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < xmax and 0 <= ny < H:
                    j = ny * W + nx
                    if fg[j] and not seen[j]:
                        seen[j] = 1; q.append(j)
        out.append((x0, y0, x1, y1, n))
    return out


def text_lines(W, H, fg, xmax, comps):
    """按行投影找出文字行区间，再用字宽区分「数字行」和「歌词行」。

    汉字宽（>=16px），数字窄（<=14px）—— 这是两者最稳的区别。
    """
    rows = []
    for y in range(H):
        base = y * W
        rows.append(sum(1 for x in range(xmax) if fg[base + x]))
    bands = []
    s = None
    for y in range(H):
        if rows[y] >= MIN_ROW_INK and s is None:
            s = y
        elif rows[y] < MIN_ROW_INK and s is not None:
            bands.append((s, y - 1)); s = None
    if s is not None:
        bands.append((s, H - 1))

    out = []
    for a, b in bands:
        if b - a + 1 < 8:
            continue
        glyphs = [c for c in comps
                  if a - 2 <= (c[1] + c[3]) / 2.0 <= b + 2
                  and 10 <= (c[3] - c[1] + 1) <= 30 and (c[4] if len(c) > 4 else 0) > 15]
        if not glyphs:
            continue
        widths = [(c[2] - c[0] + 1) for c in glyphs]
        narrow = sum(1 for w in widths if w <= 14) / float(len(widths))
        out.append(dict(a=a, b=b, kind="num" if narrow > 0.6 else "lyric"))
    return out


def digit_band(comps, a, b, xmax):
    """数字行的精确 y 区间：取高度 14~24 的字块，y0/y1 取众数（中位数会被噪声带偏，
    算矮了会把窄字「1」整段切掉，进而漏检）。"""
    ys = [(c[1], c[3]) for c in comps
          if c[2] <= xmax and 14 <= (c[3] - c[1] + 1) <= 26
          and 2 <= (c[2] - c[0] + 1) <= 40 and a - 2 <= (c[1] + c[3]) / 2.0 <= b + 2]
    if not ys:
        return None
    # 注意：most_common 返回 [(值, 计数)]，要取 [0][0] 才是值。
    return (Counter(t[0] for t in ys).most_common(1)[0][0],
            Counter(t[1] for t in ys).most_common(1)[0][0])


def digit_slots(W, fg, yt, yb, xmax):
    """数字行的列投影求每个数字的位置。过宽的块（相邻数字粘连）按 ~26px 切开。"""
    cols = [sum(1 for y in range(yt, yb + 1) if fg[y * W + x]) for x in range(xmax)]
    runs = []
    s = None
    for x in range(xmax):
        if cols[x] >= 2 and s is None:
            s = x
        elif cols[x] < 2 and s is not None:
            runs.append([s, x - 1]); s = None
    if s is not None:
        runs.append([s, xmax - 1])
    merged = []
    for r in runs:
        if merged and r[0] - merged[-1][1] <= 3:
            merged[-1][1] = r[1]
        else:
            merged.append(list(r))
    slots = []
    for r0, r1 in merged:
        w = r1 - r0 + 1
        k = max(1, round(w / 26.0))
        step = w / float(k)
        for i in range(k):
            slots.append((r0 + step * (i + 0.5), r0 + step * i, r0 + step * (i + 1) - 1))
    return sorted(s for s in slots if s[2] - s[1] >= 2)


def dots_in(comps, xmax, ya, yb):
    """窄缝里的小方块 = 八度点。"""
    r = []
    for x0, y0, x1, y1, n in comps:
        if x1 > xmax:
            continue
        if (y1 - y0 + 1) > 9 or (x1 - x0 + 1) > 9 or not (3 <= n <= 50):
            continue
        cy = (y0 + y1) / 2.0
        if ya <= cy <= yb:
            r.append(((x0 + x1) / 2.0, cy))
    return r


def main():
    ap = argparse.ArgumentParser(description="简谱图片八度点识别")
    ap.add_argument("image")
    ap.add_argument("-o", "--outdir", default=None)
    ap.add_argument("--xmax", type=int, default=None,
                    help="只扫描左侧这么宽（排除右边缘的装饰/水印），默认 90%% 宽度")
    ap.add_argument("--bands", default=None,
                    help="手动指定数字行的 y 区间，如 \"24-41,129-146\"。"
                         "自动检测的排版切分不一定准，识别结果不对时用这个兜底")
    args = ap.parse_args()

    outdir = args.outdir or os.path.dirname(os.path.abspath(args.image)) or "."
    im, W, H, _, fg = load_binary(args.image)
    xmax = args.xmax or int(W * 0.9)
    comps = components(W, H, fg, xmax)

    if args.bands:
        num_bands = []
        for part in args.bands.split(","):
            lo, _, hi = part.strip().partition("-")
            num_bands.append(dict(a=int(lo), b=int(hi), kind="num"))
        lines = num_bands
    else:
        lines = text_lines(W, H, fg, xmax, comps)
        sys.stdout.write("自动检测到的文字行（num=数字行 lyric=歌词行）：\n")
        for i, L in enumerate(lines):
            sys.stdout.write("  #%d y=%d..%d %s\n" % (i, L["a"], L["b"], L["kind"]))
        sys.stdout.write("如果切分不对，请用 --bands 手动指定数字行。\n")

    rgb = Image.open(args.image).convert("RGB")
    d = ImageDraw.Draw(rgb)
    # 无论是否手动指定数字行，都把全部文字行算一遍，用于确定「下方点」的搜索下界
    all_lines = text_lines(W, H, fg, xmax, comps)
    nums = 0
    for L in lines:
        nums += 1
        band = digit_band(comps, L["a"], L["b"], xmax)
        if not band:
            continue
        yt, yb = band
        # 下方最近一条文字行的起点（限制下方点的搜索范围，避免把歌词字头当成点）
        lyt = H
        for M in all_lines:
            if M["a"] > yb + 3:
                lyt = M["a"]; break
        slots = digit_slots(W, fg, yt, yb, xmax)
        up = dots_in(comps, xmax, max(0, yt - 14), yt - 2)
        dn = dots_in(comps, xmax, yb + 2, min(lyt - 1, yb + 14))

        seq = []
        for cx, s_, e_ in slots:
            mk = ""
            for ux, uy in up:
                if s_ - 3 <= ux <= e_ + 3:
                    mk = "^"; break
            if not mk:
                for dx, dy in dn:
                    if s_ - 3 <= dx <= e_ + 3:
                        mk = "v"; break
            seq.append("%d%s" % (round(cx), mk or "_"))
        sys.stdout.write("\n数字行 #%d  y=%d..%d  数字 %d 个  上点 %d 下点 %d\n  %s\n"
                         % (nums, yt, yb, len(slots), len(up), len(dn), "  ".join(seq)))

        d.line([(0, yt), (xmax, yt)], fill=(0, 160, 0))
        d.line([(0, yb), (xmax, yb)], fill=(0, 160, 0))
        for cx, s_, e_ in slots:
            d.line([(cx, yt - 1), (cx, yb + 1)], fill=(0, 0, 255))
        for ux, uy in up + dn:
            d.ellipse([ux - 6, uy - 6, ux + 6, uy + 6], outline=(255, 0, 0), width=2)

    p = os.path.join(outdir, "overlay.png")
    rgb.resize((W * 2, H * 2), Image.LANCZOS).save(p)
    sys.stdout.write("\noverlay -> %s\n请看一眼这张图再动手转写：红圈错位或漏圈都能看出来。\n" % p)


if __name__ == "__main__":
    main()
