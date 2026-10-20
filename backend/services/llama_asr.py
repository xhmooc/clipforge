"""本地 ASR：走 llama.cpp 的 llama-server 跑 Qwen3-ASR。

为什么是 llama.cpp 而不是 transformers：llama.cpp 用 mmap 加载 GGUF，
权重只算文件映射页，几乎不计入 Windows 的「提交量」（物理内存 + 页面文件之和）；
而 transformers 会把整份权重提交进进程私有内存，加载时还要再复制一份。
这台机器的页面文件被固定得很小，提交余量只剩几个 GB，
transformers 那条路直接被顶穿（os error 1455）。llama.cpp 天然没这个问题。

用法：第一次转写时按需把 vendor/llama/llama-server.exe 拉起来（一个进程常驻复用），
然后走它的 OpenAI 兼容接口 /v1/chat/completions + input_audio。
"""
from __future__ import annotations

import atexit
import base64
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Tuple

from .. import config

# 模型输出开头会带一句语言标记，例如 "language Chinese<asr_text>真正的文本"
_MARK = "<asr_text>"

# llama-server 的并发槽位数：转写是按音频块并行调的，槽位太少就排成一条队
PARALLEL = 4

_server: subprocess.Popen | None = None
_lock = threading.Lock()


def vendor_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "vendor"


def paths() -> Tuple[Path, Path, Path, int]:
    """定位可执行文件和两个 GGUF。设置里留空就用 vendor/ 下的默认位置。"""
    settings = config.section("asr")
    base = vendor_dir()
    binary = Path(settings.get("llama_bin") or base / "llama" / "llama-server.exe")
    model = Path(settings.get("llama_model") or base / "models" / "Qwen3-ASR-0.6B-Q8_0.gguf")
    mmproj = Path(
        settings.get("llama_mmproj") or base / "models" / "mmproj-Qwen3-ASR-0.6B-Q8_0.gguf"
    )
    return binary, model, mmproj, int(settings.get("llama_port") or 8081)


def available() -> bool:
    binary, model, mmproj, _ = paths()
    return binary.is_file() and model.is_file() and mmproj.is_file()


def _healthy(base: str) -> bool:
    try:
        with urllib.request.urlopen(base + "/health", timeout=3) as response:
            return json.loads(response.read()).get("status") == "ok"
    except Exception:  # noqa: BLE001
        return False


def _ensure_server() -> str:
    """第一次用得把 llama-server 拉起来；之后一直复用这个常驻进程。"""
    global _server
    binary, model, mmproj, port = paths()
    base = f"http://127.0.0.1:{port}"

    with _lock:
        if _healthy(base):
            return base
        if not binary.is_file():
            raise RuntimeError(
                f"没找到 llama-server：{binary}\n"
                "把 llama.cpp 的 Windows 版解压到 vendor/llama/ 下即可。"
            )
        if not model.is_file() or not mmproj.is_file():
            raise RuntimeError(f"没找到 Qwen3-ASR 的模型文件：{model} 或 {mmproj}")

        _server = subprocess.Popen(
            [
                str(binary),
                "-m", str(model),
                "--mmproj", str(mmproj),
                "--host", "127.0.0.1",
                "--port", str(port),
                "-c", "16384",
                "--parallel", str(PARALLEL),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(binary.parent),
        )

        deadline = time.time() + 300
        while time.time() < deadline:
            if _healthy(base):
                return base
            if _server.poll() is not None:
                raise RuntimeError("llama-server 启动后立刻退出了，检查一下 GGUF 路径")
            time.sleep(1.0)
        raise RuntimeError("等 llama-server 就绪超时")


def shutdown() -> None:
    """后端退出时顺手把子进程收掉，别留个孤儿占着内存。"""
    global _server
    if _server is not None and _server.poll() is None:
        _server.terminate()
        _server = None


atexit.register(shutdown)


def transcribe(data: bytes) -> str:
    """把一段 wav 字节交给 llama.cpp 转成文本。"""
    base = _ensure_server()
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "转写这段音频。"},
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": base64.b64encode(data).decode(),
                            "format": "wav",
                        },
                    },
                ],
            }
        ],
        "temperature": 0.0,
        "max_tokens": 1024,
    }
    request = urllib.request.Request(
        base + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:200]
        raise RuntimeError(f"llama.cpp 转写失败（HTTP {exc.code}）：{detail}") from exc

    text = payload["choices"][0]["message"]["content"] or ""
    # 去掉 "language Chinese<asr_text>" 这种前缀，只留正文
    return (text.split(_MARK)[-1] if _MARK in text else text).strip()
