"""SRT 字幕文件的读写。字幕是独立于视频的可编辑中间产物，必须能来回转换。"""
from __future__ import annotations

from typing import List

from .models import SubtitleCue


def format_time(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, ms = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def parse_time(value: str) -> float:
    hours, minutes, rest = value.strip().split(":")
    secs, ms = rest.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(secs) + int(ms) / 1000


def dumps(cues: List[SubtitleCue], *, use_raw: bool = False) -> str:
    """输出 SRT 文本。use_raw=True 时写 ASR 口语原稿。"""
    blocks = []
    for index, cue in enumerate(cues, start=1):
        text = cue.raw_text if (use_raw and cue.raw_text) else cue.text
        blocks.append(
            f"{index}\n{format_time(cue.start)} --> {format_time(cue.end)}\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


def loads(content: str) -> List[SubtitleCue]:
    """解析 SRT 文本为字幕列表。"""
    cues: List[SubtitleCue] = []
    for block in content.replace("\r\n", "\n").strip().split("\n\n"):
        lines = [line for line in block.split("\n") if line.strip()]
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        start, end = lines[1].split("-->")
        cues.append(
            SubtitleCue(
                index=len(cues) + 1,
                start=parse_time(start),
                end=parse_time(end),
                text="\n".join(lines[2:]).strip(),
            )
        )
    return cues
