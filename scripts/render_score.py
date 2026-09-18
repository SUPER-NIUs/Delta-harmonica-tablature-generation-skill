#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
简谱 -> 《三角洲行动》守夜人口琴谱 图片渲染器

输入：一个纯文本乐谱文件（DSL，格式见 skill 的 references/format.md）
输出：一张 PNG 图片，键盘字母装在方框里，歌词逐字对齐在方框下方。
      低音（降调）绿底、高音（升调）红底、半音黄底，长按/半长按在框内标出。

用法：
    python render_score.py song.txt -o out.png
    python render_score.py song.txt -o out.png --scale 3 --title 晴天
"""

import argparse
import os
import re
import sys

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- 音级 -> 按键
NOTE_TO_KEY = {
    "1": "Z", "2": "X", "3": "C", "4": "V",
    "5": "B", "6": "N", "7": "M",
}

# 颜色（浅底深字，保持画面干净）
C_BG = "#FFFFFF"
C_FILL_DOWN = "#93D2B8"   # 降调（低八度）绿底
C_FILL_UP = "#F2A0A0"     # 升调（高八度）红底
C_FILL_SHARP = "#F5CE80"  # 半音（中键）黄底，仅在用到时出现
C_BORDER = "#1F1F1F"
C_TEXT = "#111111"
C_LEGEND_BORDER = "#9A9A9A"
C_DIVIDER = "#DCDCDC"

TOKEN_RE = re.compile(r"^([#b]?)(\^+|_{1,2})?([0-7])([#b]?)([-.·]?)$")

FONT_SERIF_CANDIDATES = [
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
]
FONT_SANS_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
]


def find_font(candidates, size):
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


# ---------------------------------------------------------------- 乐谱解析
class Item:
    __slots__ = ("kind", "key", "lyric", "octave", "accidental", "duration")

    def __init__(self, kind, key="", lyric="", octave=0, accidental="", duration=""):
        self.kind = kind            # "note" | "rest" | "gap"
        self.key = key
        self.lyric = lyric
        self.octave = octave        # 0 中音, +1 高音, -1 低音, +2 倍高音
        self.accidental = accidental  # "" | "#" | "b"
        self.duration = duration    # "" | "-" 长按 | "." 半长按


def parse(path):
    """解析 DSL，返回 (title, [ [Item, ...], ... ])。每个内层列表是一个乐句。"""
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()

    title = ""
    phrases = []
    errors = []

    for lineno, line in enumerate(raw.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("//"):
            continue
        if s.startswith("#"):
            title = s.lstrip("#").strip().strip("《》").strip()
            continue

        if "|" in s:
            notes_part, lyric_part = s.split("|", 1)
        else:
            notes_part, lyric_part = s, ""

        note_toks = notes_part.split()
        # 歌词行里的 / 只是与音符行同步的可读分隔，不占槽位
        lyric_toks = [t for t in lyric_part.split() if t != "/"]

        items = []
        for t in note_toks:
            if t == "/":
                items.append(Item("gap"))
                continue
            if t in ("0", "-"):
                items.append(Item("rest"))
                continue
            m = TOKEN_RE.match(t)
            if not m:
                errors.append(f"第 {lineno} 行：无法识别的音符记号 “{t}”")
                continue
            acc_pre, oct_mark, deg, acc_post, dur = m.groups()
            acc = acc_pre or acc_post
            # 时长符号归一化：· / . 都是半长按，- 是长按
            if dur in ("·", ".", "。"):
                dur = "."
            elif dur:
                dur = "-"
            octave = 0
            if oct_mark:
                octave = len(oct_mark) if oct_mark[0] == "^" else -len(oct_mark)
            if octave > 2 or octave < -1:
                errors.append(f"第 {lineno} 行：{t} 超出乐器音域，已按边界处理")
                octave = max(-1, min(2, octave))
            items.append(
                Item("note", key=NOTE_TO_KEY[deg], octave=octave,
                     accidental=acc or "", duration=dur or "")
            )

        slots = [it for it in items if it.kind != "gap"]

        if lyric_toks:
            if len(lyric_toks) != len(slots):
                errors.append(
                    f"第 {lineno} 行：音符 {len(slots)} 个，歌词 {len(lyric_toks)} 个，数量不一致。"
                    f"请用 - 占位补齐。"
                )
            for it, ly in zip(slots, lyric_toks):
                it.lyric = "" if ly == "-" else ly

        if items:
            phrases.append(items)

    if errors:
        sys.stderr.write("\n".join(errors) + "\n")

    return title, phrases


# ---------------------------------------------------------------- 排版与绘制
def layout_rows(phrases, box_w, box_gap, group_gap, max_width):
    """把乐句按最大宽度折行，返回 [[Item, ...], ...]。"""
    rows = []
    for items in phrases:
        cur, cur_w = [], 0
        for it in items:
            if it.kind == "gap":
                if cur:
                    cur.append(it)
                    cur_w += group_gap
                continue
            step = box_w + box_gap
            if cur and cur_w + box_w > max_width:
                while cur and cur[-1].kind == "gap":
                    cur.pop()
                rows.append(cur)
                cur, cur_w = [], 0
            cur.append(it)
            cur_w += step
        while cur and cur[-1].kind == "gap":
            cur.pop()
        if cur:
            rows.append(cur)
    return rows


def row_width(items, box_w, box_gap, group_gap):
    w = 0
    for it in items:
        w += group_gap if it.kind == "gap" else box_w + box_gap
    return max(0, w - box_gap)


def render(title, phrases, out_path, scale=2, theme="light"):
    S = scale
    BOX = 46 * S
    BOX_GAP = 6 * S
    GROUP_GAP = 34 * S
    LINE_W = 900 * S
    MARGIN = 56 * S
    LEGEND_W = 205 * S
    LEGEND_GAP = 56 * S
    ROW_PITCH = 122 * S
    LYRIC_DY = 14 * S
    LEGEND_ITEM_H = 40 * S
    LEGEND_ITEM_GAP = 28 * S

    f_title = find_font(FONT_SANS_CANDIDATES, 50 * S)
    f_letter = find_font(FONT_SERIF_CANDIDATES, 30 * S)
    f_lyric = find_font(FONT_SERIF_CANDIDATES, 23 * S)
    f_legend = find_font(FONT_SANS_CANDIDATES, 27 * S)

    rows = layout_rows(phrases, BOX, BOX_GAP, GROUP_GAP, LINE_W)
    if not rows:
        raise SystemExit("乐谱是空的：没解析出任何音符。")

    max_row_w = max(row_width(r, BOX, BOX_GAP, GROUP_GAP) for r in rows)

    used_accidental = any(
        it.accidental for r in rows for it in r if it.kind == "note"
    )
    legend = [("降调", C_FILL_DOWN, "fill"),
              ("升调", C_FILL_UP, "fill")]
    if used_accidental:
        legend.append(("半音", C_FILL_SHARP, "fill"))
    legend += [("半长按", None, "dot"), ("长按", None, "dash")]

    canvas_w = int(MARGIN * 2 + max_row_w + LEGEND_GAP + LEGEND_W)
    title_block = (70 * S) if title else 0
    first_row_top = int(MARGIN + title_block + 34 * S)
    row_bottom = first_row_top + (len(rows) - 1) * ROW_PITCH + BOX + LYRIC_DY + 32 * S
    legend_bottom = (first_row_top - 8 * S
                     + len(legend) * (LEGEND_ITEM_H + LEGEND_ITEM_GAP) - LEGEND_ITEM_GAP)
    canvas_h = int(max(row_bottom, legend_bottom) + MARGIN)

    img = Image.new("RGB", (canvas_w, canvas_h), C_BG)
    d = ImageDraw.Draw(img)

    # 标题（逐字加字距，模仿简谱标题排法）
    if title:
        text = "《 " + " ".join(title) + " 》"
        d.text((canvas_w // 2, MARGIN + 20 * S), text,
               font=f_title, fill=C_TEXT, anchor="mm")
        d.line([(MARGIN, first_row_top - 22 * S), (canvas_w - MARGIN, first_row_top - 22 * S)],
               fill=C_DIVIDER, width=max(1, S))

    # 乐谱行
    for ri, items in enumerate(rows):
        y = int(first_row_top + ri * ROW_PITCH)
        x = MARGIN
        for it in items:
            if it.kind == "gap":
                x += GROUP_GAP
                continue

            if it.kind == "rest":
                bx = x
                d.rectangle([bx, y, bx + BOX, y + BOX], fill=C_BG,
                            outline=C_LEGEND_BORDER, width=max(1, S))
                if it.lyric:
                    d.text((bx + BOX / 2, y + BOX + LYRIC_DY), it.lyric,
                           font=f_lyric, fill=C_TEXT, anchor="ma")
                x += BOX + BOX_GAP
                continue

            fill = C_BG
            if it.accidental:
                fill = C_FILL_SHARP
            elif it.octave > 0:
                fill = C_FILL_UP
            elif it.octave < 0:
                fill = C_FILL_DOWN

            bx = x
            d.rectangle([bx, y, bx + BOX, y + BOX], fill=fill,
                        outline=C_BORDER, width=max(1, S))

            label = it.key
            if it.octave == 2 and it.key == "Z":
                label = ","
            mark = it.duration  # "" | "-" 长按 | "." 半长按

            # 时长符号用图形绘制，避免字体缺字
            mark_w = 17 * S if mark else 0
            lw = d.textlength(label, font=f_letter)
            start = bx + (BOX - lw - mark_w) / 2
            # 逗号字形贴在基线上，单独上提一点才和字母视觉居中
            ly_off = -9 * S if label == "," else 0
            d.text((start, y + BOX / 2 + ly_off), label, font=f_letter,
                   fill=C_TEXT, anchor="lm")
            if mark == "-":
                dx = start + lw + 4 * S
                d.rectangle([dx, y + BOX / 2 - 1.6 * S, dx + 13 * S, y + BOX / 2 + 1.6 * S],
                            fill=C_TEXT)
            elif mark == ".":
                dcx = start + lw + 8 * S
                r = 3.2 * S
                d.ellipse([dcx - r, y + BOX / 2 - r, dcx + r, y + BOX / 2 + r], fill=C_TEXT)

            if it.lyric:
                d.text((bx + BOX / 2, y + BOX + LYRIC_DY), it.lyric,
                       font=f_lyric, fill=C_TEXT, anchor="ma")

            x += BOX + BOX_GAP

    # 图例（右上角，与第一行乐谱顶部对齐）
    sw_w, sw_h = 96 * S, LEGEND_ITEM_H
    lx = canvas_w - MARGIN - LEGEND_W + 8 * S
    ly = first_row_top - 8 * S
    for name, color, kind in legend:
        d.rectangle([lx, ly, lx + sw_w, ly + sw_h], fill=color or C_BG,
                    outline=C_LEGEND_BORDER, width=max(1, S))
        cx, cy = lx + sw_w / 2, ly + sw_h / 2
        if kind == "dot":
            r = 5.5 * S
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=C_TEXT)
        elif kind == "dash":
            d.rectangle([cx - 17 * S, cy - 2 * S, cx + 17 * S, cy + 2 * S], fill=C_TEXT)
        d.text((lx + sw_w + 22 * S, cy), " ".join(name), font=f_legend,
               fill=C_TEXT, anchor="lm")
        ly += sw_h + LEGEND_ITEM_GAP

    img.save(out_path, "PNG")
    return out_path, img.size


def main():
    ap = argparse.ArgumentParser(description="简谱 -> 三角洲口琴谱图片")
    ap.add_argument("input", help="简谱 DSL 文本文件")
    ap.add_argument("-o", "--output", default="harmonica_score.png")
    ap.add_argument("--scale", type=int, default=2, help="清晰度倍数，默认 2")
    ap.add_argument("--title", default=None, help="覆盖曲名")
    args = ap.parse_args()

    title, phrases = parse(args.input)
    if args.title:
        title = args.title
    path, size = render(title, phrases, args.output, scale=max(1, args.scale))
    print(f"OK {path} {size[0]}x{size[1]}")


if __name__ == "__main__":
    main()
