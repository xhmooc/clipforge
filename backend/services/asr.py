"""语音转写：走云端 ASR 模型。

网关的 ASR 只回纯文本、没有时间戳，所以**时间轴和断句都得我们自己造**：
  1. 先用 FFmpeg 检测停顿，把音频切成语句级小块 → 并行转写
     （并行顺带解决另一个问题：单次转写约等于音频实时长度，4 小时串行要跑 4 小时）
  2. 块内部再用检测到的停顿，把文字对齐到真实的语音段上

关键点：断句必须靠「检测」而不是靠字数。按字数硬切会把一句话拦腰截断，
比如"加厚羽绒服，充绒量" / "很高，冬天穿着" 这种断裂。
"""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Optional, Tuple

from .. import config
from ..models import SubtitleCue
from . import cloud, ffmpeg, llama_asr

# 切块节奏：切太碎调用次数暴涨，切太大时间戳就不准了
MIN_CHUNK = 4.0
TARGET_CHUNK = 14.0
MAX_CHUNK = 28.0

# 停顿检测参数：短于这个时长的静音不算停顿
PAUSE_SECONDS = 0.25
PAUSE_DB = -35

STRONG_BREAK = re.compile(r"[。！？!?；;…]+")
SOFT_BREAK = re.compile(r"[，、,]+")
EDGE_PUNCT = "，。、；：！？,.;:!?… \t"

# 一行字幕的目标字数上限
MAX_LINE_CHARS = 22


def plan_chunks(duration: float, cut_points: List[float]) -> List[Tuple[float, float]]:
    """把整段音频切成一块块，优先在停顿处切。"""
    points = sorted(p for p in cut_points if 0.0 < p < duration)
    chunks: List[Tuple[float, float]] = []
    start = 0.0
    for point in points:
        length = point - start
        if length >= MAX_CHUNK or length >= TARGET_CHUNK:
            chunks.append((start, point))
            start = point
    if duration - start > 0.01:
        chunks.append((start, duration))

    # 尾巴太短就并进前一块，免得留下半句话的碎片
    if len(chunks) > 1 and chunks[-1][1] - chunks[-1][0] < MIN_CHUNK:
        last = chunks.pop()
        chunks[-1] = (chunks[-1][0], last[1])
    return chunks


def speech_runs(start: float, end: float, pauses: List[float]) -> List[Tuple[float, float]]:
    """把 [start, end) 按停顿切成一段段连续的语音。"""
    points = [start] + [p for p in pauses if start < p < end] + [end]
    return [(points[i], points[i + 1]) for i in range(len(points) - 1)]


def _cuts(text: str, redundant: bool = False) -> List[int]:
    """所有能下刀的位置。redundant=True 时连句内标点也算。"""
    pattern = re.compile(f"{STRONG_BREAK.pattern}|{SOFT_BREAK.pattern}") if redundant else SOFT_BREAK
    return [m.end() for m in pattern.finditer(text) if 0 < m.end() < len(text)]


def _align_cuts(text: str, durations: List[float]) -> List[int]:
    """挑切点：让每段文字的字数占比，尽量贴合它对应语音段的时长占比。

    这是断句质量的关键——切点要落在真实停顿上，而不是按字数平均切。
    句末标点优先，实在没有才退到句内标点。
    """
    if len(durations) <= 1:
        return []

    strong = {m.end() for m in STRONG_BREAK.finditer(text) if 0 < m.end() < len(text)}
    candidates = _cuts(text, redundant=True)
    total_chars = len(text)
    total_duration = sum(durations) or 1.0

    selected: List[int] = []
    previous = 0
    prefix = 0.0
    for duration in durations[:-1]:
        prefix += duration
        target = total_chars * prefix / total_duration
        available = [c for c in candidates if c > previous]
        if not available:
            break
        pool = [c for c in available if c in strong] or available
        best = min(pool, key=lambda c: abs(c - target))
        selected.append(best)
        previous = best
    return selected


def _balanced_split(text: str, cuts: List[int]) -> List[str]:
    """从中间附近最合适的标点处切，切完两边继续切，直到每段都不超长。

    比"满 N 字就硬切"好得多——硬切会造出"充绒量"/"很高"这种断裂。
    """
    if len(text) <= MAX_LINE_CHARS or not cuts:
        return [text]

    middle = len(text) / 2
    best = min(cuts, key=lambda c: abs(c - middle))
    left = _balanced_split(text[:best], [c for c in cuts if c < best])
    right = _balanced_split(text[best:], [c - best for c in cuts if c > best])
    return left + right


def _lines(text: str, run_start: float, run_end: float) -> List[SubtitleCue]:
    """一段语音里的文字 → 若干条字幕，时间按字数分配。"""
    text = text.strip(EDGE_PUNCT)
    if not text:
        return []

    parts = [part.strip(EDGE_PUNCT) for part in _balanced_split(text, _cuts(text))]
    parts = [part for part in parts if part]
    if not parts:
        return []

    total = sum(len(part) for part in parts)
    span = max(0.0, run_end - run_start)
    cursor = run_start
    cues: List[SubtitleCue] = []
    for part in parts:
        share = span * len(part) / total
        cues.append(
            SubtitleCue(index=0, start=cursor, end=cursor + share, text=part, raw_text=part)
        )
        cursor += share
    return cues


def _cues_for_chunk(text: str, start: float, end: float, pauses: List[float]) -> List[SubtitleCue]:
    """块内对齐：把文字切点落到检测出来的停顿上。"""
    text = text.strip()
    if not text:
        return []

    runs = speech_runs(start, end, pauses)
    cuts = _align_cuts(text, [run_end - run_start for run_start, run_end in runs])
    if len(cuts) != len(runs) - 1:
        # 块内没检测到停顿（或者切点不够）：退回整块按字数分配
        return _lines(text, start, end)

    bounds = [0] + cuts + [len(text)]
    cues: List[SubtitleCue] = []
    for index, (run_start, run_end) in enumerate(runs):
        cues.extend(_lines(text[bounds[index]:bounds[index + 1]], run_start, run_end))
    return cues


def _engine(data: bytes) -> str:
    """按设置里的 provider 决定这一块音频交给谁转写。"""
    if config.section("asr").get("provider") == "llama":
        return llama_asr.transcribe(data)
    return cloud.transcribe_audio(data)


def transcribe(
    audio_path: str,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> List[SubtitleCue]:
    """把音频转成带时间戳的字幕列表。

    on_progress(已完成块数, 总块数) 每转写完一块回调一次，用来给前端报进度。
    """
    path = str(audio_path)
    duration = ffmpeg.duration(path)
    if duration <= 0:
        return []

    settings = config.section("asr")
    # 本地是 llama-server 的并发槽位数；云端受网关限额，走环境变量配置
    workers = llama_asr.PARALLEL if settings.get("provider") == "llama" else config.ASR_CONCURRENCY

    silences = ffmpeg.detect_silences(path, noise_db=PAUSE_DB, min_duration=PAUSE_SECONDS)
    pauses = [(begin + end) / 2 for begin, end in silences]
    chunks = plan_chunks(duration, pauses)

    total = len(chunks)
    completed = 0
    counter = threading.Lock()

    def work(chunk: Tuple[float, float]) -> Tuple[Tuple[float, float], str]:
        nonlocal completed
        start, end = chunk
        audio = ffmpeg.slice_audio_bytes(path, start, end)
        text = _engine(audio)
        if on_progress is not None:
            with counter:
                completed += 1
                on_progress(completed, total)
        return chunk, text

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(work, chunks))

    cues: List[SubtitleCue] = []
    for (start, end), text in sorted(results, key=lambda item: item[0][0]):
        cues.extend(_cues_for_chunk(text, start, end, pauses))

    for index, cue in enumerate(cues, start=1):
        cue.index = index
    return cues
