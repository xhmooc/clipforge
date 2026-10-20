"""数据模型：字幕、片段、商品分段、工程。"""
from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class SubtitleCue(BaseModel):
    """一条字幕。

    text 是给观众看的精简版（用于烧录），raw_text 是 ASR 的口语原稿（用于对时间轴）。
    """

    index: int
    start: float
    end: float
    text: str
    raw_text: str = ""


class Clip(BaseModel):
    """工作台上手动框出来的片段（入点 / 出点）。"""

    id: str
    start: float
    end: float
    title: str = ""
    mirror: bool = False  # 水平镜像，带货常用来改"同一件衣服换个样"


class Sticker(BaseModel):
    """贴在成片上的贴纸/角标（图片）。

    位置和大小都按画面的比例来存，这样换分辨率也不会跑偏。
    """

    id: str
    file: str  # workspace/<工程>/stickers 下的文件名
    x: float = 0.5  # 0=贴左边，0.5=居中，1=贴右边
    y: float = 0.5
    scale: float = 0.25  # 相对成片宽度的比例
    # 出现在成片的哪一段时间；end 为 0 表示从 start 一直显示到片尾
    start: float = 0.0
    end: float = 0.0
    # 时间轴上的泳道序号，越大越靠上层（成片里也就盖在越上面）
    track: int = 0


class HighlightClip(BaseModel):
    """商品讲解段落里的一个高光片段（由 LLM 自动挑出）。"""

    id: str
    start: float
    end: float
    title: str = ""
    reason: str = ""


class ProductSegment(BaseModel):
    """一段商品讲解。带货直播的价值就建立在这个分段上。"""

    id: str
    name: str
    start: float
    end: float
    clips: List[HighlightClip] = Field(default_factory=list)


class Project(BaseModel):
    """一场直播回放的完整工程。"""

    id: str
    name: str
    source_file: str = ""
    preview_file: str = ""
    duration: float = 0.0
    source_width: int = 0
    source_height: int = 0
    # landscape = 横屏（16:9 那种）；portrait = 竖屏。出片时两条路的做法不一样
    orientation: str = "landscape"
    status: str = "created"  # created | transcribing | transcribed | rendering | done | failed
    message: str = ""
    progress: int = 0  # 转写进度百分比，用来让用户看到"确实在动"
    subtitles: List[SubtitleCue] = Field(default_factory=list)
    clips: List[Clip] = Field(default_factory=list)
    renders: List["Render"] = Field(default_factory=list)
    segments: List[ProductSegment] = Field(default_factory=list)
    stickers: List[Sticker] = Field(default_factory=list)
    fonts: List[str] = Field(default_factory=list)  # 上传字体的族名，渲染时喂给 libass
    concat_preview: str = ""  # 所有片段串起来的预览片文件名
    # 字幕样式，渲染时写进 ASS
    subtitle_font: str = "Microsoft YaHei"
    subtitle_size: int = 18
    subtitle_color: str = "#FFFFFF"
    # 字幕框在画面里的落点（0~1，锚点是框底边的中点），可以在预览上直接拖
    subtitle_x: float = 0.5
    subtitle_y: float = 0.7917
    # 字幕框宽度占画面的比例。它决定一行能放几个字——框越宽，文字越舒展
    subtitle_width: float = 0.86
    created_at: str = ""


class Render(BaseModel):
    """一个已经导出成片的切片。"""

    clip_id: str
    title: str
    file: str  # 相对 workspace/<工程>/renders 的文件名


class SubtitleUpdate(BaseModel):
    """前端回传的字幕。"""

    subtitles: List[SubtitleCue]


class ClipUpdate(BaseModel):
    """前端回传的片段。"""

    clips: List[Clip]


class AnalyzeRequest(BaseModel):
    """「AI 找高光」的参数：这次想要几段（0 = 不限）。"""

    limit: int = 3


class StickerUpdate(BaseModel):
    """前端回传的贴纸列表（含位置和大小）。"""

    stickers: List[Sticker] = Field(default_factory=list)


class StyleUpdate(BaseModel):
    """前端回传的字幕样式和位置。"""

    subtitle_font: str
    subtitle_size: int
    subtitle_color: str
    subtitle_x: float = 0.5
    subtitle_y: float = 0.7917
    subtitle_width: float = 0.86
