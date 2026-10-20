"""字体工具：从 ttf/otf/ttc 文件里读出字体族名。

libass 是按「族名」找字体的，光把文件丢进 fontsdir 还不够——
force_style 里的 FontName 必须填对，否则字幕还是回退到默认字体。
"""
from __future__ import annotations

from pathlib import Path

NAME_FAMILY = 1
NAME_FULL = 4


def family_name(path: str | Path) -> str:
    """读出字体族名（比如「思源黑体 CN」）。读不出来就抛异常让调用方提示用户。"""
    data = Path(path).read_bytes()
    if data[:4] == b"ttcf":
        # 字体集合里塞了好几个字体，文件头第 12 字节是第一个的偏移
        data = data[int.from_bytes(data[12:16], "big") :]
    if len(data) < 12:
        raise ValueError("字体文件不完整")

    tables = int.from_bytes(data[4:6], "big")
    name_offset = 0
    for index in range(tables):
        record = 12 + index * 16
        if data[record : record + 4] == b"name":
            name_offset = int.from_bytes(data[record + 8 : record + 12], "big")
            break
    if not name_offset:
        raise ValueError("字体里没有 name 表，认不出族名")

    count = int.from_bytes(data[name_offset + 2 : name_offset + 4], "big")
    storage = name_offset + int.from_bytes(data[name_offset + 4 : name_offset + 6], "big")

    best, best_score = "", -1
    for index in range(count):
        record = name_offset + 6 + index * 12
        platform = int.from_bytes(data[record : record + 2], "big")
        language = int.from_bytes(data[record + 4 : record + 6], "big")
        name_id = int.from_bytes(data[record + 6 : record + 8], "big")
        length = int.from_bytes(data[record + 8 : record + 10], "big")
        offset = int.from_bytes(data[record + 10 : record + 12], "big")
        if name_id not in (NAME_FAMILY, NAME_FULL):
            continue

        raw = data[storage + offset : storage + offset + length]
        if platform == 3:  # Windows 记录是 UTF-16BE；简体中文用 0x804
            text = raw.decode("utf-16-be", errors="ignore")
            score = (2 if name_id == NAME_FAMILY else 0) + (1 if language in (0x409, 0x804) else 0)
        elif platform == 1:  # Mac 记录是单字节
            text = raw.decode("latin-1", errors="ignore")
            score = 1 if name_id == NAME_FAMILY else 0
        else:
            continue

        text = text.strip()
        if text and score > best_score:
            best, best_score = text, score

    if not best:
        raise ValueError("字体里没写族名")
    return best
