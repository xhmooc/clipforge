"""把字幕写成 ASS 文件，好让字幕能精确定位。

之前走的是「SRT + FFmpeg 的 force_style」，问题是 SRT 只能表达「一段文字 + 起止时间」，
位置只能靠 style 里的 Alignment / Margin 去猜，没法自由落点。自己写 ASS 之后，
每个字幕行可以带 \\pos，字幕就能像贴纸一样拖到画面任意位置。

PlayRes 保持 216×384（就是成片 1080×1920 的 9:16 比例）：
字号 18、描边 3、底部边距 80 这些数值跟原来完全一致，所以成片观感不会变，
只是多了一个可以指定的位置。
"""
from __future__ import annotations

from typing import List, Optional, Sequence

# 和成片 9:16 同比例的坐标系；预览里的字号换算也是按 216 这个宽度来的
PLAY_RES_X = 216
PLAY_RES_Y = 384

# 默认位置：水平居中、底部留 80（等于原来的 Alignment=2 + MarginV=80）
DEFAULT_X = 0.5
DEFAULT_Y = 1 - 80 / PLAY_RES_Y
# 字幕框默认占画面宽度的比例（框是可以拖的，这是初始值）
DEFAULT_WIDTH = 0.86

# 折行预算的下限，防止字号特别大时每行只放得下半个字
MIN_BUDGET = 2.0
# 折行时优先在这些标点后面断，避免把词切一半
BREAK_AFTER = "，。、；：！？,.;:!?…"

# WrapStyle 2 = 只认显式写出来的 \N，不做自动折行。
# 因为我们给每行都加了 \pos，libass 那边就不自动折行了，长句子会直接冲出画面。
HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},{primary},&H000000FF,{outline},&H00000000,-1,0,0,0,100,100,0,0,1,3,1,2,{margin:.1f},{margin:.1f},80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _char_width(char: str) -> float:
    """粗略的字宽：中日韩字符按一个字算，其余（英文、数字、半角标点）算半个。"""
    return 1.0 if ord(char) > 0x2E7F else 0.5


def wrap(text: str, budget: float = MIN_BUDGET) -> List[str]:
    """按显示宽度折行，优先在标点后断，实在没有就在预算处硬断。"""
    lines: List[str] = []
    current: List[str] = []
    width = 0.0

    for char in text:
        size = _char_width(char)
        if current and width + size > budget:
            cut = len(current)
            for index in range(len(current) - 1, 0, -1):
                if current[index - 1] in BREAK_AFTER:
                    cut = index
                    break
            lines.append("".join(current[:cut]))
            current = current[cut:]
            width = sum(_char_width(item) for item in current)
            if not current:
                width = 0.0
        current.append(char)
        width += size

    if current:
        lines.append("".join(current))
    return lines or [""]


def ass_color(value: str) -> str:
    """#RRGGBB → ASS 的 &HAABBGGRR（注意是 BGR 倒序）。"""
    digits = (value or "").lstrip("#")
    if len(digits) != 6:
        digits = "FFFFFF"
    return f"&H00{digits[4:6]}{digits[2:4]}{digits[0:2]}".upper()


def timestamp(seconds: float) -> str:
    """ASS 的时间格式是 H:MM:SS.cc（百分之一秒）。"""
    centis = int(round(max(0.0, seconds) * 100))
    hours, centis = divmod(centis, 360000)
    minutes, centis = divmod(centis, 6000)
    whole, centis = divmod(centis, 100)
    return f"{hours}:{minutes:02d}:{whole:02d}.{centis:02d}"


def escape(text: str) -> str:
    """花括号在 ASS 里是样式覆盖块的定界符，必须去掉；换行要写成 \\N。"""
    cleaned = (text or "").replace("{", "").replace("}", "")
    return cleaned.replace("\r\n", "\\N").replace("\n", "\\N").strip()


def dumps(
    cues: Sequence,
    *,
    font: str,
    size: int,
    color: str,
    x: float,
    y: float,
    width: float = DEFAULT_WIDTH,
) -> str:
    """把字幕列表写成 ASS 文本。

    width 是字幕框占画面宽度的比例（0.15~1）。它同时决定两件事：
      - 折行预算：一行放得下几个字 = 框宽 / 字号，所以框变宽、或字号变小，一行就能塞更多字
      - 左右边距：和框宽对齐，免得 libass 又按自己的宽度再折一次行
    """
    points = PLAY_RES_X
    size = max(1, int(size))
    box = max(0.15, min(1.0, width)) * points
    margin = max(0.0, (points - box) / 2)
    budget = max(MIN_BUDGET, box / size)

    header = HEADER.format(
        width=PLAY_RES_X,
        height=PLAY_RES_Y,
        font=font or "Microsoft YaHei",
        size=size,
        primary=ass_color(color),
        outline="&H00000000",
        margin=margin,
    )

    px = max(0.0, min(1.0, x)) * PLAY_RES_X
    py = max(0.0, min(1.0, y)) * PLAY_RES_Y
    position = f"{{\\an2\\pos({px:.1f},{py:.1f})}}"

    lines: List[str] = []
    for cue in cues:
        text = escape(cue.text)
        if not text:
            continue
        body = "\\N".join(wrap(text, budget))
        lines.append(
            f"Dialogue: 0,{timestamp(cue.start)},{timestamp(cue.end)},Default,,0,0,0,,"
            f"{position}{body}"
        )
    return header + "\n".join(lines) + "\n"


def position_from(project) -> tuple:
    """取工程里存的字幕框位置和宽度，缺省就是底部居中、占 86% 宽。"""
    x = getattr(project, "subtitle_x", None)
    y = getattr(project, "subtitle_y", None)
    width = getattr(project, "subtitle_width", None)
    return (
        DEFAULT_X if x is None else float(x),
        DEFAULT_Y if y is None else float(y),
        DEFAULT_WIDTH if width is None else float(width),
    )
