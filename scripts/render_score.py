#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
简谱 -> 《三角洲行动》守夜人口琴谱 渲染器

输入：一个纯文本乐谱文件（DSL，格式见 SKILL.md 第四节）
输出：PNG 图片（需要 Pillow + 一个中文字体）
      或 HTML（零依赖，字体交给浏览器，云环境/无字体时用它）

      低音（降调）绿底、高音（升调）红底、半音黄底，长按/半长按在框内标出。

行为约定（给调用它的 agent 看）：
    音符数与歌词数不一致、记号看不懂、超出音域 —— 这些一律只是**提示**，
    照常出图，提示打印在 stdout 并标注"无需处理"。不要去"修"它们。

用法：
    python render_score.py song.txt -o out.png            # PNG，需要 Pillow
    python render_score.py song.txt -o out.html           # HTML，零依赖
    python render_score.py song.txt -o out.png --scale 3 --title 晴天
    python render_score.py song.txt -o out.png --font-serif /path/to/NotoSerifCJK.ttc
"""

import argparse
import glob
import os
import re
import sys

# Pillow 惰性导入：输出 HTML 时完全不需要它，云环境常没装
Image = ImageDraw = ImageFont = None


def _load_pil():
    global Image, ImageDraw, ImageFont
    if Image is None:
        try:
            from PIL import Image as _I, ImageDraw as _D, ImageFont as _F
        except ImportError:
            sys.stderr.write(
                "ERROR: 生成 PNG 需要 Pillow，当前环境没有。\n"
                "  方案一： pip install pillow\n"
                "  方案二： 改出 HTML（零依赖，中文一定能显示）： -o out.html\n"
            )
            raise SystemExit(3)
        Image, ImageDraw, ImageFont = _I, _D, _F
    return Image, ImageDraw, ImageFont

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
    # Windows
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    # macOS
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    # Linux（常见发行版）
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
]
FONT_SANS_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]

# 各平台常见的 CJK 字体目录，硬编码路径之外的兜底扫描
CJK_FONT_GLOBS = [
    "/usr/share/fonts/**/Noto*CJK*.tt*",
    "/usr/share/fonts/**/Noto*SC*.otf",
    "/usr/share/fonts/**/SourceHan*",
    "/usr/share/fonts/**/wqy-*.tt[cf]",
    "/usr/share/fonts/**/DroidSansFallback*.ttf",
    "/usr/local/share/fonts/**/*.tt[cf]",
    "~/.fonts/**/*.tt[cf]",
    "~/Library/Fonts/*Songti*",
    "~/Library/Fonts/*PingFang*",
]
_font_scan_cache = None


def scan_cjk_fonts():
    global _font_scan_cache
    if _font_scan_cache is None:
        found = []
        for pattern in CJK_FONT_GLOBS:
            found.extend(sorted(glob.glob(os.path.expanduser(pattern), recursive=True)))
        _font_scan_cache = found
    return _font_scan_cache


def find_font(candidates, size, override=None):
    """返回 (字体对象, 字体路径)。找不到时返回 (默认位图字体, None) —— 中文会变方块。"""
    paths = []
    if override:
        paths.append(override)
    paths.extend(candidates)
    paths.extend(scan_cjk_fonts())
    for path in paths:
        if path and os.path.exists(path):
            try:
                return ImageFont.truetype(path, size), path
            except Exception:
                continue
    return ImageFont.load_default(), None


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
    """解析 DSL，返回 (title, phrases, notes)。

    notes 是给人看的**提示**，不是错误：脚本照常出图。
    音符数与歌词数不一致属于正常情况（弹唱谱常带装饰音/伴奏音），一律只提示、不拦。
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()

    title = ""
    phrases = []
    notes = []

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
                notes.append(f"第 {lineno} 行：跳过了看不懂的记号 “{t}”")
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
                notes.append(f"第 {lineno} 行：{t} 超出乐器音域，已就近处理")
                octave = max(-1, min(2, octave))
            items.append(
                Item("note", key=NOTE_TO_KEY[deg], octave=octave,
                     accidental=acc or "", duration=dur or "")
            )

        slots = [it for it in items if it.kind != "gap"]

        if lyric_toks:
            extra = len(lyric_toks) - len(slots)
            if extra > 0:
                notes.append(f"第 {lineno} 行：歌词比音符多 {extra} 个，多出的已忽略"
                             f"（弹唱谱常见，属正常）")
            elif extra < 0:
                notes.append(f"第 {lineno} 行：有 {-extra} 个音符没配歌词，已留空（属正常）")
            for it, ly in zip(slots, lyric_toks):
                it.lyric = "" if ly == "-" else ly

        if items:
            phrases.append(items)

    return title, phrases, notes


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


# ---------------------------------------------------------------- 公共：配色与图例
def color_for(it):
    """一个槽位该用哪种底色：plain / down / up / sharp / rest"""
    if it.kind == "rest":
        return "rest"
    if it.accidental:
        return "sharp"
    if it.octave > 0:
        return "up"
    if it.octave < 0:
        return "down"
    return "plain"


def build_legend(rows):
    """图例条目 [(名称, 类型)]，类型为 down/up/sharp/dot/dash。半音按需出现。"""
    items = [("降调", "down"), ("升调", "up")]
    if any(it.accidental for r in rows for it in r if it.kind == "note"):
        items.append(("半音", "sharp"))
    items += [("半长按", "dot"), ("长按", "dash")]
    return items


def box_label(it):
    """槽位里显示的字符：倍高音 1 在琴上是逗号键。"""
    if it.octave == 2 and it.key == "Z":
        return ","
    return it.key


def render(title, phrases, out_path, scale=2, font_serif=None, font_sans=None):
    S = scale
    _load_pil()
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

    f_title, _ = find_font(FONT_SANS_CANDIDATES, 50 * S, font_sans)
    f_letter, _ = find_font(FONT_SERIF_CANDIDATES, 30 * S, font_serif)
    f_lyric, _ = find_font(FONT_SERIF_CANDIDATES, 23 * S, font_serif)
    f_legend, _ = find_font(FONT_SANS_CANDIDATES, 27 * S, font_sans)

    rows = layout_rows(phrases, BOX, BOX_GAP, GROUP_GAP, LINE_W)
    if not rows:
        raise SystemExit("乐谱是空的：没解析出任何音符。")

    max_row_w = max(row_width(r, BOX, BOX_GAP, GROUP_GAP) for r in rows)

    legend = [(name, {"down": C_FILL_DOWN, "up": C_FILL_UP,
                      "sharp": C_FILL_SHARP}.get(kind), kind)
              for name, kind in build_legend(rows)]

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

            fill = {"down": C_FILL_DOWN, "up": C_FILL_UP,
                    "sharp": C_FILL_SHARP}.get(color_for(it), C_BG)

            bx = x
            d.rectangle([bx, y, bx + BOX, y + BOX], fill=fill,
                        outline=C_BORDER, width=max(1, S))

            label = box_label(it)
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


# ---------------------------------------------------------------- HTML 输出（零依赖）
# 云环境/无中文字体/没装 Pillow 时用这条路径：字体交给浏览器，
# 任何中文系统都能正常显示，手机上也自适应换行。
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root{
    --down:#93D2B8; --up:#F2A0A0; --sharp:#F5CE80;
    --ink:#111111; --soft:#9A9A9A; --line:#DCDCDC;
  }
  *{box-sizing:border-box}
  html,body{margin:0}
  body{
    background:#fff;color:var(--ink);
    font-family:"PingFang SC","Hiragino Sans GB","Microsoft YaHei",
                "Noto Sans CJK SC","Source Han Sans SC","WenQuanYi Micro Hei",sans-serif;
    padding:26px 18px 44px;-webkit-text-size-adjust:100%;
  }
  .sheet{max-width:1180px;margin:0 auto}
  .title{
    margin:0 0 16px;text-align:center;font-weight:700;letter-spacing:.16em;
    font-family:"Songti SC","SimSun","Noto Serif CJK SC","Source Han Serif SC",serif;
    font-size:__F_TITLE__px;
  }
  .rule{height:1px;background:var(--line);margin:0 0 28px}
  .wrap{display:flex;gap:36px;align-items:flex-start}
  .rows{flex:1 1 auto;min-width:0}
  .row{
    display:flex;flex-wrap:wrap;align-content:flex-start;
    column-gap:__BOX_GAP__px;row-gap:__ROW_GAP__px;
    margin:0 0 __ROW_MARGIN__px;
  }
  .box{
    position:relative;flex:0 0 auto;
    width:__BOX__px;height:__BOX__px;
    display:inline-flex;align-items:center;justify-content:center;gap:3px;
    border:1px solid #1F1F1F;border-radius:2px;background:#fff;
  }
  .box.down{background:var(--down)}
  .box.up{background:var(--up)}
  .box.sharp{background:var(--sharp)}
  .box.rest{background:#fff;border-color:var(--soft);border-style:dashed}
  .key{
    font-family:Georgia,"Times New Roman","Songti SC",serif;
    font-size:__F_KEY__px;font-weight:700;line-height:1;
  }
  .mark{display:inline-block;background:var(--ink);flex:0 0 auto}
  .mark.dash{width:__DASH_W__px;height:__DASH_H__px;border-radius:1px}
  .mark.dot{width:__DOT__px;height:__DOT__px;border-radius:50%}
  .lyric{
    position:absolute;top:100%;left:50%;transform:translateX(-50%);
    padding-top:__LYR_DY__px;white-space:nowrap;
    font-family:"Songti SC","SimSun","Noto Serif CJK SC",serif;
    font-size:__F_LYRIC__px;line-height:1;
  }
  .spacer{flex:0 0 auto;width:__GROUP_GAP__px}
  .legend{flex:0 0 auto;display:flex;flex-direction:column;gap:__LEG_GAP__px}
  .lg{display:flex;align-items:center;gap:14px}
  .sw{
    width:__SW__px;height:__SW__px;background:#fff;
    border:1px solid var(--soft);border-radius:2px;
    display:inline-flex;align-items:center;justify-content:center;
  }
  .sw.down{background:var(--down);border-color:#1F1F1F}
  .sw.up{background:var(--up);border-color:#1F1F1F}
  .sw.sharp{background:var(--sharp);border-color:#1F1F1F}
  .lg-name{font-size:__F_LEG__px;letter-spacing:.22em}
  .hint{margin:34px 0 0;color:#8A8A8A;font-size:13px;line-height:2}
  .hint b{color:#5A5A5A;font-weight:600}
  @media (max-width:900px){
    .wrap{flex-direction:column;gap:28px}
    .legend{flex-direction:row;flex-wrap:wrap;gap:16px 24px}
  }
  @media print{
    body{padding:0}
    .hint{display:none}
  }
</style>
</head>
<body>
<div class="sheet">
__TITLE_BLOCK____RULE__<div class="wrap">
<div class="rows">
__SCORE__
</div>
<aside class="legend">
__LEGEND__
</aside>
</div>
<p class="hint">
  <b>怎么按</b>：单字 = 直接敲对应字母键；<b>低音（绿）</b> = 按住<b>鼠标左键</b>再敲；
  <b>高音（红）</b> = 按住<b>鼠标右键</b>再敲；<b>半音（黄）</b> = 按住<b>鼠标中键</b>再敲。<br>
  <b>时长</b>：框内有横杠 = 长按不放；框内有点 = 半长按；空白框 = 休止，跳过。
</p>
</div>
</body>
</html>
"""


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _html_box(it):
    cls = color_for(it)
    if it.kind == "gap":
        return '<span class="spacer"></span>'
    parts = []
    if it.kind == "note":
        parts.append(f'<span class="key">{_esc(box_label(it))}</span>')
        if it.duration == "-":
            parts.append('<span class="mark dash"></span>')
        elif it.duration == ".":
            parts.append('<span class="mark dot"></span>')
    if it.lyric:
        parts.append(f'<span class="lyric">{_esc(it.lyric)}</span>')
    return f'<span class="box {cls}">' + "".join(parts) + "</span>"


def _html_legend_item(name, kind):
    if kind == "dot":
        sw = '<span class="sw"><span class="mark dot"></span></span>'
    elif kind == "dash":
        sw = '<span class="sw"><span class="mark dash"></span></span>'
    else:
        sw = f'<span class="sw {kind}"></span>'
    return f'<div class="lg">{sw}<span class="lg-name">{_esc(" ".join(name))}</span></div>'


def render_html(title, phrases, out_path, scale=2):
    S = max(1, scale)
    BOX = 26 * S
    vals = {
        "BOX": BOX,
        "BOX_GAP": max(4, round(BOX * 0.14)),
        "GROUP_GAP": round(BOX * 0.62),
        "F_KEY": round(BOX * 0.52),
        "F_LYRIC": round(BOX * 0.44),
        "F_LEG": round(BOX * 0.30),
        "F_TITLE": round(BOX * 0.86),
        "LYR_DY": round(BOX * 0.20),
        "DASH_W": round(BOX * 0.30),
        "DASH_H": max(2, round(BOX * 0.065)),
        "DOT": round(BOX * 0.16),
        "SW": round(BOX * 0.47),
        "LEG_GAP": round(BOX * 0.30),
    }
    vals["ROW_GAP"] = vals["LYR_DY"] + vals["F_LYRIC"] + round(BOX * 0.38)
    vals["ROW_MARGIN"] = round(BOX * 0.56)

    rows = [r for r in phrases if any(it.kind != "gap" for it in r)]
    if not rows:
        raise SystemExit("乐谱是空的：没解析出任何音符。")

    legend = [_html_legend_item(n, k) for n, k in build_legend(
        [r for r in rows])]

    html = HTML_TEMPLATE
    for key, value in vals.items():
        html = html.replace(f"__{key}__", str(value))
    html = html.replace("__TITLE__", _esc(title or "三角洲口琴谱"))
    if title:
        html = html.replace("__TITLE_BLOCK__",
                            f'<div class="title">{_esc("《 " + " ".join(title) + " 》")}</div>')
        html = html.replace("__RULE__", '<div class="rule"></div>')
    else:
        html = html.replace("__TITLE_BLOCK__", "").replace("__RULE__", "")
    html = html.replace("__SCORE__", "\n".join(
        '<div class="row">' + "".join(_html_box(it) for it in row) + "</div>"
        for row in rows))
    html = html.replace("__LEGEND__", "\n".join(legend))

    # newline="\n"：Windows 的文本模式会把 \n 翻成 \r\n，HTML 里没必要，
    # 还会让 git 的 CRLF 归一化把逐字节校验搞出假报错。
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    return out_path


def main():
    ap = argparse.ArgumentParser(description="简谱 -> 三角洲口琴谱（PNG 或 HTML）")
    ap.add_argument("input", help="简谱 DSL 文本文件")
    ap.add_argument("-o", "--output", default="harmonica_score.png",
                    help="输出文件；.html 结尾则输出零依赖网页版")
    ap.add_argument("--scale", type=int, default=2, help="清晰度倍数，默认 2")
    ap.add_argument("--title", default=None, help="覆盖曲名")
    ap.add_argument("--font-serif", default=None, help="[PNG] 指定衬线中文字体路径")
    ap.add_argument("--font-sans", default=None, help="[PNG] 指定无衬线中文字体路径")
    args = ap.parse_args()

    title, phrases, notes = parse(args.input)
    if args.title:
        title = args.title
    scale = max(1, args.scale)

    # 提示走 stdout 并写明"无需处理"：走 stderr 会被 agent 当成报错，
    # 进而触发"回头核对、反复修谱"的死循环。
    if notes:
        print(f"提示（不影响出图，无需处理）：共 {len(notes)} 条")
        for n in notes:
            print("  " + n)

    if args.output.lower().endswith((".html", ".htm")):
        path = render_html(title, phrases, args.output, scale=scale)
        print(f"OK {path} html")
    else:
        path, size = render(title, phrases, args.output, scale=scale,
                            font_serif=args.font_serif, font_sans=args.font_sans)
        print(f"OK {path} {size[0]}x{size[1]}")


if __name__ == "__main__":
    main()
