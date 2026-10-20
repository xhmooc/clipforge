"""出片：把工作台框好的片段，切成竖屏并烧上对应时间段的字幕。"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import List

from .. import config
from ..models import Project, Render, Sticker, SubtitleCue
from . import ass, ffmpeg
from . import project as projects

RENDERS_DIR = "renders"
STICKERS_DIR = "stickers"
FONTS_DIR = "fonts"
PREVIEW_PARTS_DIR = "_preview_parts"
CONCAT_PREVIEW = "concat_preview.mp4"


def _cues_for_clip(cues: List[SubtitleCue], start: float, end: float) -> List[SubtitleCue]:
    """挑出落在片段里的字幕，并把时间平移到片段内（从 0 开始）。"""
    picked: List[SubtitleCue] = []
    for cue in cues:
        if cue.end <= start or cue.start >= end:
            continue
        picked.append(
            SubtitleCue(
                index=len(picked) + 1,
                start=max(0.0, cue.start - start),
                end=min(end, cue.end) - start,
                text=cue.text,
                raw_text=cue.raw_text,
            )
        )
    return picked


def _safe_name(title: str, index: int) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", (title or "").strip()).strip("_")
    return f"{index:03d}_{cleaned}" if cleaned else f"clip_{index:03d}"


def _stickers_for_clip(project: Project, clip) -> List[dict]:
    """把贴纸的全局时间窗换算成片段内的时间，完全不落在这一段里的直接丢掉。

    拖上时间轴之后贴纸才有出现/消失的概念；换算这一步不做的话，
    一个 10 秒的片段会照着 3 小时的时间轴去算 enable 表达式。
    end 为 0 表示「一直到片尾」。
    """
    items: List[dict] = []
    # 按泳道排序：编号大的后贴，在成片里就盖在上面——和剪辑软件里上层轨压下层一致
    for sticker in sorted(project.stickers, key=lambda item: item.track):
        ends_inside = 0 < sticker.end <= clip.start
        starts_after = sticker.start >= clip.end
        if ends_inside or starts_after:
            continue
        rel_end = (min(sticker.end, clip.end) - clip.start) if sticker.end > 0 else 0.0
        items.append(
            {
                # 滤镜链是在工程目录下跑的，所以这里给相对文件名
                "file": f"{STICKERS_DIR}/{sticker.file}",
                "x": sticker.x,
                "y": sticker.y,
                "scale": sticker.scale,
                "start": max(0.0, sticker.start - clip.start),
                "end": max(0.0, rel_end),
            }
        )
    return items


def _render_one(
    project: Project, directory: Path, source: Path, clip, out_rel: str, sub_rel: str
) -> None:
    """渲染单个片段，字幕按片段内的时间轴单独落一份 ASS（位置写在里面）。"""
    cues = _cues_for_clip(project.subtitles, clip.start, clip.end)
    if cues:
        x, y, width = ass.position_from(project)
        (directory / sub_rel).write_text(
            ass.dumps(
                cues,
                font=project.subtitle_font,
                size=project.subtitle_size,
                color=project.subtitle_color,
                x=x,
                y=y,
                width=width,
            ),
            encoding="utf-8",
        )
    ffmpeg.render_clip(
        directory=directory,
        source=source.name,
        start=clip.start,
        end=clip.end,
        out_name=out_rel,
        sub_name=sub_rel if cues else None,
        portrait=project.orientation == "portrait",
        mirror=clip.mirror,
        stickers=_stickers_for_clip(project, clip),
        fonts_dir=FONTS_DIR if project.fonts else None,
    )


def render_all(project: Project) -> List[Render]:
    """把工程里所有片段渲染成竖屏切片。"""
    if not project.clips:
        raise RuntimeError("还没有片段，先在播放器上标出入点、出点，再加为片段")

    directory = projects.project_dir(project.id)
    source = projects.find_source(directory)
    if source is None:
        raise RuntimeError("找不到原始视频，无法渲染")

    (directory / RENDERS_DIR).mkdir(exist_ok=True)
    (directory / STICKERS_DIR).mkdir(exist_ok=True)
    (directory / FONTS_DIR).mkdir(exist_ok=True)

    results: List[Render] = []
    for index, clip in enumerate(project.clips, start=1):
        file_name = f"{_safe_name(clip.title, index)}.mp4"
        _render_one(
            project, directory, source, clip, f"{RENDERS_DIR}/{file_name}", f"clip_{index:03d}.ass"
        )
        results.append(Render(clip_id=clip.id, title=clip.title, file=file_name))

    _copy_to_export_dir(directory, results)
    return results


def render_concat_preview(project: Project) -> str:
    """按当前顺序把所有片段串成一条预览片，用于导出前整体过一遍。

    各段规格一致，所以拼接是 -c copy，不用重编码。
    """
    if not project.clips:
        raise RuntimeError("还没有片段，先框出至少一段再看拼接")

    directory = projects.project_dir(project.id)
    source = projects.find_source(directory)
    if source is None:
        raise RuntimeError("找不到原始视频，无法渲染")

    for name in (PREVIEW_PARTS_DIR, STICKERS_DIR, FONTS_DIR):
        (directory / name).mkdir(exist_ok=True)

    parts: List[str] = []
    for index, clip in enumerate(project.clips, start=1):
        rel = f"{PREVIEW_PARTS_DIR}/{index:03d}.mp4"
        _render_one(project, directory, source, clip, rel, f"clip_{index:03d}.ass")
        parts.append(rel)

    ffmpeg.concat(parts, CONCAT_PREVIEW, directory)
    return CONCAT_PREVIEW


def _copy_to_export_dir(directory: Path, results: List[Render]) -> None:
    """设置里填了导出目录的话，再把成片复制一份过去，方便直接拿去发布。"""
    target = (config.section("export").get("dir") or "").strip()
    if not target:
        return
    destination = Path(target)
    destination.mkdir(parents=True, exist_ok=True)
    for item in results:
        shutil.copy2(directory / RENDERS_DIR / item.file, destination / item.file)
