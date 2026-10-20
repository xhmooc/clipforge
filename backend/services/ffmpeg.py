"""FFmpeg 封装：定位可执行文件、探测时长、抽音频、转码预览、烧字幕。"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

_executable: str | None = None

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)")
_SILENCE_START = re.compile(r"silence_start: ([\d.]+)")
_SILENCE_END = re.compile(r"silence_end: ([\d.]+)")
_VIDEO_CODEC_RE = re.compile(r"Stream #\d+:\d+.*?: Video: ([a-zA-Z0-9_]+)")
_AUDIO_CODEC_RE = re.compile(r"Stream #\d+:\d+.*?: Audio: ([a-zA-Z0-9_]+)")
# 视频流那行里第一个 WxH 就是分辨率；后面 SAR/DAR 里也有数字，所以要用边界卡住
_SIZE_RE = re.compile(r"Video: \S+.*?(\d{2,5})x(\d{2,5})[\s,\[]")

# 浏览器 <video> 能直接播的编码
BROWSER_VIDEO = {"h264"}
BROWSER_AUDIO = {"aac", "mp3"}


def ffmpeg_exe() -> str:
    """优先用系统 ffmpeg，没有就退回 imageio-ffmpeg 自带的静态二进制。"""
    global _executable
    if _executable is None:
        _executable = shutil.which("ffmpeg") or _from_imageio()
    return _executable


def _from_imageio() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="ignore"
    )


def run_bytes(args: list[str]) -> bytes:
    """执行 ffmpeg 并把 stdout 直接当二进制拿回来，省掉临时文件。"""
    return subprocess.run(args, capture_output=True).stdout


def detect_silences(
    path: str, *, noise_db: int = -35, min_duration: float = 0.3
) -> list[tuple[float, float]]:
    """找出静音区间，用来定位自然的断句点。"""
    result = run(
        [
            ffmpeg_exe(), "-hide_banner", "-i", str(path),
            "-af", f"silencedetect=noise={noise_db}dB:d={min_duration}",
            "-f", "null", "-",
        ]
    )
    log = result.stderr or ""
    starts = [float(v) for v in _SILENCE_START.findall(log)]
    ends = [float(v) for v in _SILENCE_END.findall(log)]
    return list(zip(starts, ends))


def slice_audio_bytes(
    path: str, start: float, end: float, *, sample_rate: int = 16000
) -> bytes:
    """切出 [start, end) 一段音频，直接以 wav 字节返回。"""
    return run_bytes(
        [
            ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-i", str(path),
            "-t", f"{max(0.0, end - start):.3f}",
            "-ac", "1", "-ar", str(sample_rate), "-f", "wav", "-",
        ]
    )


def duration(path: str) -> float:
    """读取媒体时长（秒）。ffprobe 不一定有，所以直接解析 ffmpeg -i 的输出。"""
    result = run([ffmpeg_exe(), "-hide_banner", "-i", str(path)])
    match = _DURATION_RE.search(result.stderr or "")
    if not match:
        return 0.0
    hours, minutes, seconds, frac = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + float(f"0.{frac}")


def probe_size(path: str) -> tuple[int, int]:
    """读出画面分辨率，用来判断源片是竖屏还是横屏。"""
    result = run([ffmpeg_exe(), "-hide_banner", "-i", str(path)])
    match = _SIZE_RE.search(result.stderr or "")
    if not match:
        return (0, 0)
    return (int(match.group(1)), int(match.group(2)))


def extract_audio(video: str, out_wav: str, *, sample_rate: int = 16000) -> str:
    """抽 16k 单声道 wav，喂给 ASR。"""
    run(
        [
            ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video),
            "-vn", "-ac", "1", "-ar", str(sample_rate),
            "-f", "wav", str(out_wav),
        ]
    )
    return str(out_wav)


def transcode_for_preview(source: str, target: str, *, height: int = 720) -> str:
    """转成浏览器认识的 H.264 MP4。

    带货回放常是 FLV/TS，<video> 原生放不了，必须先转码。
    """
    run(
        [
            ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-vf", f"scale=-2:{height}",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(target),
        ]
    )
    return str(target)


def probe_codecs(path: str) -> tuple:
    """读出视频/音频编码名，用来判断能不能只重封装。"""
    result = run([ffmpeg_exe(), "-hide_banner", "-i", str(path)])
    log = result.stderr or ""
    video = _VIDEO_CODEC_RE.search(log)
    audio = _AUDIO_CODEC_RE.search(log)
    return (video.group(1) if video else ""), (audio.group(1) if audio else "")


def prepare_for_preview(source: str, target: str) -> str:
    """为浏览器准备可播放的预览，尽量少干活。

    4 小时的回放做整片重编码是几十倍到上百倍的浪费：
      - 本来就是 MP4/H.264 的：原片浏览器就能播，什么都不用做
      - FLV/TS 里装的是 H.264/AAC：只需 -c copy 换封装，秒级完成
      - 编码浏览器不认（HEVC/AV1 等）：才真的重编码
    返回实际走了哪条路，方便在界面上告诉用户。
    """
    video, audio = probe_codecs(source)
    playable = video in BROWSER_VIDEO and (not audio or audio in BROWSER_AUDIO)

    if playable and str(source).lower().endswith(".mp4"):
        return "skip"  # 原片直接能播，预览接口会回退到它

    if playable:
        run(
            [
                ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(source),
                "-c", "copy", "-movflags", "+faststart",
                str(target),
            ]
        )
        return "remux"

    transcode_for_preview(source, target)
    return "transcode"


def _sticker_enable(sticker: dict, duration: float) -> str:
    """贴纸的出现时间窗。

    整段都在的话不写 enable——多一个条件表达式，滤镜图就多一分开销。
    时间都是片段内的相对时间，因为渲染是带 -ss 跑的，输出从 0 开始计时。
    """
    start = float(sticker.get("start", 0.0))
    end = float(sticker.get("end", 0.0))
    if start <= 0 and (end <= 0 or end >= duration):
        return ""
    if end > 0:
        return f":enable='between(t,{start:.3f},{min(end, duration):.3f})'"
    return f":enable='gte(t,{start:.3f})'"


def render_clip(
    directory,
    source: str,
    start: float,
    end: float,
    out_name: str,
    sub_name: str | None = None,
    *,
    width: int = 1080,
    height: int = 1920,
    portrait: bool = False,
    mirror: bool = False,
    stickers: list | None = None,
    fonts_dir: str | None = None,
) -> str:
    """把一段画面切成竖屏，挂上贴纸，再烧上字幕。

    两种源片的做法不一样：
      - 横屏（16:9 那种直播）：「模糊背景铺满 + 居中正片」。模糊是在缩小的图上做的，
        再放大回来，比直接对 1080p 做高斯模糊便宜得多。
      - 竖屏：本来就该竖着看，直接裁满画布，省掉模糊那一整条链。

    cwd 设成工程目录、只用相对文件名，是为了绕开 Windows 上 subtitles 滤镜的路径转义地狱。
    """
    duration = max(0.1, end - start)
    stickers = stickers or []
    parts: list[str] = []

    if portrait:
        chain = (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1"
        )
        if mirror:
            chain += ",hflip"
        parts.append(f"{chain}[base]")
    else:
        parts.append(
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,scale=180:320,gblur=sigma=4,"
            f"scale={width}:{height},setsar=1[bg]"
        )
        foreground = f"[0:v]scale={width}:-2,setsar=1" + (",hflip" if mirror else "")
        parts.append(f"{foreground}[fg]")
        parts.append("[bg][fg]overlay=(W-w)/2:(H-h)/2[base]")

    label = "base"
    for index, sticker in enumerate(stickers, start=1):
        target = max(16, int(width * float(sticker.get("scale", 0.25))))
        parts.append(f"[{index}:v]scale={target}:-2[st{index}]")
        parts.append(
            f"[{label}][st{index}]overlay="
            f"x=(W-w)*{float(sticker.get('x', 0.5)):.4f}:"
            f"y=(H-h)*{float(sticker.get('y', 0.5)):.4f}"
            f"{_sticker_enable(sticker, duration)}[ov{index}]"
        )
        label = f"ov{index}"

    if sub_name:
        # 样式和位置都写在 ASS 里了，这里不再用 force_style 去覆盖
        fonts = f":fontsdir={fonts_dir}" if fonts_dir else ""
        parts.append(f"[{label}]subtitles={sub_name}{fonts}[v]")
        label = "v"

    args = [
        ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", source,
    ]
    for sticker in stickers:
        # 单张图片只有一帧，-loop 1 让它铺满整段；真正的时长靠输出侧 -t 收口
        args += ["-loop", "1", "-i", sticker["file"]]
    args += ["-filter_complex", ";".join(parts), "-map", f"[{label}]", "-map", "0:a?"]
    if stickers:
        args += ["-t", f"{duration:.3f}"]
    args += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        out_name,
    ]

    result = subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="ignore",
        cwd=str(directory),
    )
    if result.returncode != 0:
        raise RuntimeError(f"渲染失败: {(result.stderr or '').strip()[-400:]}")
    return str(out_name)


def concat(files: list[str], out_name: str, directory) -> str:
    """把几段同规格的成片直接接成龙。

    各段编码参数完全一致，所以能用 -c copy 串，不必重新编码——拼一条预览片基本是秒级。
    """
    listing = "_concat.txt"
    (Path(directory) / listing).write_text(
        "".join(f"file '{name}'\n" for name in files), encoding="utf-8"
    )
    result = subprocess.run(
        [
            ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", listing,
            "-c", "copy", "-movflags", "+faststart",
            out_name,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        cwd=str(directory),
    )
    if result.returncode != 0:
        raise RuntimeError(f"拼接失败: {(result.stderr or '').strip()[-400:]}")
    return str(out_name)
