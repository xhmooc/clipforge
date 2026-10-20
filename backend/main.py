"""ClipWork 后端入口。

跑法（在 clipwork 目录下）：
    .venv\\Scripts\\python.exe -m uvicorn backend.main:app --reload
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, subtitles
from .models import (
    AnalyzeRequest,
    ClipUpdate,
    Project,
    Sticker,
    StickerUpdate,
    StyleUpdate,
    SubtitleUpdate,
)
from .services import asr, ffmpeg, fonts, highlight, render
from .services import project as projects

app = FastAPI(title="ClipWork", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- 后台任务

def _job_prepare(project_id: str) -> None:
    """上传后立刻转一份浏览器能放的预览 MP4。

    带货回放常是 FLV，<video> 放不了；而且转码比转写快得多，
    所以先让用户能看片，再慢慢等 ASR。
    """
    project = projects.load(project_id)
    directory = projects.project_dir(project_id)
    source = projects.find_source(directory)
    if source is None:
        return

    project.status = "preparing"
    projects.save(project)

    duration = project.duration
    width = height = 0
    preview_file = project.preview_file
    status = "ready"
    try:
        duration = ffmpeg.duration(str(source))
        width, height = ffmpeg.probe_size(str(source))
        mode = ffmpeg.prepare_for_preview(str(source), str(directory / projects.PREVIEW))
        # skip 表示原片浏览器就能播，预览接口会直接回退到原片
        preview_file = "" if mode == "skip" else projects.PREVIEW
        message = {
            "skip": "预览就绪：原片本身浏览器就能播，没做任何处理",
            "remux": "预览就绪：只换了封装（秒级），没有重新编码",
            "transcode": "预览就绪：编码浏览器不认，已重新转码",
        }[mode]
    except Exception as exc:
        message = f"预览转码失败: {exc}"
        status = "failed"

    # 重新读一遍再落盘。导入之后马上点转写是常见操作，
    # 转写任务会在预览转码期间往同一个工程文件里写状态和字幕；
    # 如果直接拿「开始时的那份快照」写回去，就会把转写的成果盖掉
    # （实测确实会把状态打回 ready、字幕清零）。
    fresh = projects.load(project_id)
    fresh.duration = duration or fresh.duration
    if width and height:
        fresh.source_width, fresh.source_height = width, height
        # 竖屏片子不用加模糊背景，出片时和横屏走两条滤镜链
        fresh.orientation = "portrait" if height > width else "landscape"
    fresh.preview_file = preview_file
    fresh.message = message
    if fresh.status == "preparing":
        fresh.status = status
    projects.save(fresh)


def _job_transcribe(project_id: str) -> None:
    project = projects.load(project_id)
    directory = projects.project_dir(project_id)
    source = projects.find_source(directory)
    project.status = "transcribing"
    project.message = ""
    project.progress = 0
    projects.save(project)

    write_lock = threading.Lock()

    def report(done: int, total: int) -> None:
        # 只在百分比真的变了才落盘，免得把工程文件写爆
        percent = int(done * 100 / max(1, total))
        with write_lock:
            if percent == project.progress:
                return
            project.progress = percent
            projects.save(project)

    try:
        audio = directory / projects.AUDIO
        ffmpeg.extract_audio(str(source), str(audio))
        cues = asr.transcribe(str(audio), on_progress=report)

        project.subtitles = cues
        project.progress = 100
        if cues:
            project.duration = project.duration or cues[-1].end

        (directory / projects.SUBTITLES).write_text(
            subtitles.dumps(cues), encoding="utf-8"
        )
        (directory / projects.SUBTITLES_RAW).write_text(
            subtitles.dumps(cues, use_raw=True), encoding="utf-8"
        )
        project.status = "transcribed"
    except Exception as exc:
        project.status = "failed"
        project.message = f"转写失败: {exc}"
    projects.save(project)


def _job_concat_preview(project_id: str) -> None:
    """把当前片段按顺序串成一条预览片，导出前先整体过一遍。"""
    project = projects.load(project_id)
    project.status = "previewing"
    project.message = ""
    projects.save(project)
    try:
        project.concat_preview = render.render_concat_preview(project)
        project.status = "ready"
        project.message = "拼接预览已就绪，播放器里放的就是拼接结果"
    except Exception as exc:
        project.status = "failed"
        project.message = f"拼接预览失败: {exc}"
    projects.save(project)


def _job_render(project_id: str) -> None:
    project = projects.load(project_id)
    project.status = "rendering"
    project.message = ""
    projects.save(project)
    try:
        project.renders = render.render_all(project)
        project.status = "done"
    except Exception as exc:
        project.status = "failed"
        project.message = f"渲染失败: {exc}"
    projects.save(project)


def _job_analyze(project_id: str) -> None:
    """让模型读转写稿，找出商品讲解段。段数由设置里的 highlight.limit 决定。"""
    project = projects.load(project_id)
    project.status = "analyzing"
    project.message = ""
    projects.save(project)
    try:
        limit = int(config.section("highlight").get("limit", 3) or 0)
        project.segments = highlight.find_highlights(project, limit=limit)
        project.status = "analyzed"
        if not project.segments:
            project.message = "这场里没听出正在介绍商品的片段"
    except Exception as exc:
        project.status = "failed"
        project.message = f"找高光失败: {exc}"
    projects.save(project)


# ---------------------------------------------------------------- 工具

def _ensure_orientation(project: Project) -> None:
    """老工程是在「方向识别」之前建的，这里懒补一次，省得还要重新导入。

    补一次就写回工程文件，之后每次打开都命中缓存，不会反复探测。
    """
    if project.source_width and project.source_height:
        return
    source = projects.find_source(projects.project_dir(project.id))
    if source is None:
        return
    width, height = ffmpeg.probe_size(str(source))
    if not width or not height:
        return
    project.source_width, project.source_height = width, height
    project.orientation = "portrait" if height > width else "landscape"
    projects.save(project)


def _get(project_id: str) -> Project:
    try:
        project = projects.load(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _ensure_orientation(project)
    return project


# ---------------------------------------------------------------- 接口

@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "ffmpeg": ffmpeg.ffmpeg_exe()}


def _mask(key: str) -> str:
    """密钥回给前端时只露尾巴，避免明文在页面上到处传。"""
    if not key:
        return ""
    return "*" * 8 + key[-4:] if len(key) > 4 else "*" * len(key)


@app.get("/api/settings")
def get_settings() -> dict:
    # 不是每个分区都有密钥（导出、高光都没有），有才回显，免得凭空塞一个字段
    return {
        name: (
            {**values, "api_key": _mask(values["api_key"])} if "api_key" in values else dict(values)
        )
        for name, values in config.load().items()
    }


@app.put("/api/settings")
def update_settings(payload: dict) -> dict:
    current = config.load()
    for name, values in current.items():
        incoming = payload.get(name)
        if not isinstance(incoming, dict):
            continue
        for key, value in incoming.items():
            if key not in values:
                continue
            # 前端回传的是掩码，说明用户没动这个字段，保留原来的密钥
            if key == "api_key" and "*" in str(value):
                continue
            values[key] = value
    config.save(current)
    return get_settings()


@app.post("/api/projects", response_model=Project)
def create_project(background: BackgroundTasks, file: UploadFile = File(...)) -> Project:
    project = projects.create_project(file.filename or "replay.mp4", file.file)
    background.add_task(_job_prepare, project.id)
    return project


@app.get("/api/projects", response_model=list[Project])
def list_projects() -> list[Project]:
    return projects.list_projects()


@app.get("/api/projects/{project_id}", response_model=Project)
def get_project(project_id: str) -> Project:
    return _get(project_id)


@app.post("/api/projects/{project_id}/transcribe", response_model=Project)
def start_transcribe(project_id: str, background: BackgroundTasks) -> Project:
    project = _get(project_id)
    project.status = "transcribing"
    projects.save(project)
    background.add_task(_job_transcribe, project_id)
    return project


@app.patch("/api/projects/{project_id}/subtitles", response_model=Project)
def update_subtitles(project_id: str, payload: SubtitleUpdate) -> Project:
    project = _get(project_id)
    project.subtitles = payload.subtitles
    projects.save(project)
    (projects.project_dir(project_id) / projects.SUBTITLES).write_text(
        subtitles.dumps(project.subtitles), encoding="utf-8"
    )
    return project


@app.get("/api/projects/{project_id}/video")
def get_video(project_id: str) -> FileResponse:
    directory = projects.project_dir(project_id)
    preview = directory / projects.PREVIEW
    if preview.exists():
        return FileResponse(preview, media_type="video/mp4")
    source = projects.find_source(directory)
    if source is None:
        raise HTTPException(status_code=404, detail="视频不存在")
    return FileResponse(source)


@app.patch("/api/projects/{project_id}/clips", response_model=Project)
def update_clips(project_id: str, payload: ClipUpdate) -> Project:
    project = _get(project_id)
    project.clips = payload.clips
    return projects.save(project)


@app.patch("/api/projects/{project_id}/style", response_model=Project)
def update_style(project_id: str, payload: StyleUpdate) -> Project:
    project = _get(project_id)
    project.subtitle_font = payload.subtitle_font
    project.subtitle_size = payload.subtitle_size
    project.subtitle_color = payload.subtitle_color
    project.subtitle_x = payload.subtitle_x
    project.subtitle_y = payload.subtitle_y
    project.subtitle_width = payload.subtitle_width
    return projects.save(project)


@app.post("/api/projects/{project_id}/stickers", response_model=Project)
def upload_sticker(project_id: str, file: UploadFile = File(...)) -> Project:
    """上传一张贴纸/角标，直接挂到工程上。"""
    project = _get(project_id)
    name = Path(file.filename or "sticker.png").name
    if Path(name).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        raise HTTPException(status_code=400, detail="贴纸只支持 png / jpg / webp / gif")

    directory = projects.project_dir(project_id) / render.STICKERS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / name).open("wb") as handle:
        shutil.copyfileobj(file.file, handle)

    project.stickers = [*project.stickers, Sticker(id=uuid.uuid4().hex[:8], file=name)]
    return projects.save(project)


@app.patch("/api/projects/{project_id}/stickers", response_model=Project)
def update_stickers(project_id: str, payload: StickerUpdate) -> Project:
    """保存贴纸的位置和大小。"""
    project = _get(project_id)
    project.stickers = payload.stickers
    return projects.save(project)


@app.delete("/api/projects/{project_id}/stickers/{name}", response_model=Project)
def delete_sticker(project_id: str, name: str) -> Project:
    project = _get(project_id)
    project.stickers = [item for item in project.stickers if item.file != name]
    (projects.project_dir(project_id) / render.STICKERS_DIR / name).unlink(missing_ok=True)
    return projects.save(project)


@app.get("/api/projects/{project_id}/stickers/{name}")
def get_sticker(project_id: str, name: str) -> FileResponse:
    base = (projects.project_dir(project_id) / render.STICKERS_DIR).resolve()
    target = (base / name).resolve()
    if target.parent != base or not target.is_file():
        raise HTTPException(status_code=404, detail="贴纸不存在")
    return FileResponse(target)


@app.post("/api/projects/{project_id}/fonts", response_model=Project)
def upload_font(project_id: str, file: UploadFile = File(...)) -> Project:
    """上传字体。libass 认的是族名，所以这里先把名字读出来再存。"""
    project = _get(project_id)
    name = Path(file.filename or "font.ttf").name
    if Path(name).suffix.lower() not in {".ttf", ".otf", ".ttc"}:
        raise HTTPException(status_code=400, detail="字体只支持 ttf / otf / ttc")

    directory = projects.project_dir(project_id) / render.FONTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    with target.open("wb") as handle:
        shutil.copyfileobj(file.file, handle)

    try:
        family = fonts.family_name(target)
    except Exception as exc:  # noqa: BLE001
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"认不出这个字体的名字：{exc}") from exc

    if family not in project.fonts:
        project.fonts = [*project.fonts, family]
        project.subtitle_font = family  # 传完就切过去，省得用户还得再去选一次
    return projects.save(project)


@app.post("/api/projects/{project_id}/preview", response_model=Project)
def start_preview(project_id: str, background: BackgroundTasks) -> Project:
    project = _get(project_id)
    project.status = "previewing"
    project.message = ""
    projects.save(project)
    background.add_task(_job_concat_preview, project_id)
    return project


@app.get("/api/projects/{project_id}/concat")
def get_concat_preview(project_id: str) -> FileResponse:
    target = projects.project_dir(project_id) / render.CONCAT_PREVIEW
    if not target.is_file():
        raise HTTPException(status_code=404, detail="还没有拼接预览")
    return FileResponse(target, media_type="video/mp4")


@app.post("/api/projects/{project_id}/analyze", response_model=Project)
def start_analyze(
    project_id: str, background: BackgroundTasks, payload: AnalyzeRequest | None = None
) -> Project:
    project = _get(project_id)
    if payload is not None:
        # 顺手把段数记到设置里，下次打开还是这个数
        current = config.load()
        current["highlight"]["limit"] = max(0, min(50, int(payload.limit)))
        config.save(current)
    project.status = "analyzing"
    project.message = ""
    projects.save(project)
    background.add_task(_job_analyze, project_id)
    return project


@app.post("/api/projects/{project_id}/render", response_model=Project)
def start_render(project_id: str, background: BackgroundTasks) -> Project:
    project = _get(project_id)
    project.status = "rendering"
    project.message = ""
    projects.save(project)
    background.add_task(_job_render, project_id)
    return project


@app.get("/api/projects/{project_id}/renders/{name}")
def get_render(project_id: str, name: str) -> FileResponse:
    base = (projects.project_dir(project_id) / render.RENDERS_DIR).resolve()
    target = (base / name).resolve()
    # 只允许取 renders 目录下的单个文件，挡住路径穿越
    if target.parent != base or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target, media_type="video/mp4")


@app.post("/api/projects/{project_id}/reveal")
def reveal_export(project_id: str) -> dict:
    """在资源管理器里打开导出目录，省得用户自己去翻路径。"""
    configured = (config.section("export").get("dir") or "").strip()
    target = (
        Path(configured) if configured else projects.project_dir(project_id) / render.RENDERS_DIR
    )
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"目录还不存在：{target}")
    subprocess.Popen(["explorer", str(target)])
    return {"ok": True, "dir": str(target)}


# 前端构建产物由后端直接托管，这样整个应用只需要一条启动命令
FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"
if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="workbench")
