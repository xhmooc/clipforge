"""工程管理：workspace 下每场回放一个独立目录，中间产物全部落盘。"""
from __future__ import annotations

import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable, List

from ..models import Project

WORKSPACE = Path(__file__).resolve().parents[2] / "workspace"

SOURCE_PREFIX = "source"
PREVIEW = "preview.mp4"
AUDIO = "audio.wav"
SUBTITLES = "subtitles.srt"
SUBTITLES_RAW = "subtitles.raw.srt"
META = "project.json"


def ensure_workspace() -> Path:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    return WORKSPACE


def project_dir(project_id: str) -> Path:
    return ensure_workspace() / project_id


def find_source(directory: Path) -> Path | None:
    """原始视频统一命名为 source.<原扩展名>。"""
    for item in sorted(directory.glob(f"{SOURCE_PREFIX}.*")):
        return item
    return None


def create_project(file_name: str, stream) -> Project:
    project_id = uuid.uuid4().hex[:12]
    directory = project_dir(project_id)
    directory.mkdir(parents=True, exist_ok=True)

    suffix = Path(file_name).suffix.lower() or ".mp4"
    source = directory / f"{SOURCE_PREFIX}{suffix}"
    with source.open("wb") as handle:
        shutil.copyfileobj(stream, handle)

    project = Project(
        id=project_id,
        name=Path(file_name).stem,
        source_file=source.name,
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    save(project)
    return project


def save(project: Project) -> Project:
    directory = project_dir(project.id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / META).write_text(project.model_dump_json(indent=2), encoding="utf-8")
    return project


def load(project_id: str) -> Project:
    meta = project_dir(project_id) / META
    if not meta.exists():
        raise FileNotFoundError(f"工程不存在: {project_id}")
    return Project.model_validate_json(meta.read_text(encoding="utf-8"))


def list_projects() -> List[Project]:
    ensure_workspace()
    items: Iterable[Path] = sorted(
        (d for d in WORKSPACE.iterdir() if d.is_dir()), reverse=True
    )
    projects: List[Project] = []
    for directory in items:
        meta = directory / META
        if not meta.exists():
            continue
        try:
            projects.append(Project.model_validate_json(meta.read_text(encoding="utf-8")))
        except Exception:
            continue
    return projects
