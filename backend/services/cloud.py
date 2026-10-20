"""云端模型客户端。

网关是 OpenAI 兼容的，但音频模型不走 /audio/* 端点，而是走 /chat/completions
把音频塞进 message 里。这一点是实测出来的，不是猜的。
"""
from __future__ import annotations

import base64
from typing import List, Optional

import httpx

from .. import config


def _headers(section: str) -> dict:
    return {"Authorization": f"Bearer {config.api_key(section)}"}


def chat(
    messages: List[dict],
    *,
    model: Optional[str] = None,
    temperature: float = 0.0,
    timeout: float = 300.0,
) -> str:
    """调用文本模型，返回回复正文。"""
    settings = config.section("llm")
    response = httpx.post(
        f"{settings['base_url'].rstrip('/')}/chat/completions",
        headers=_headers("llm"),
        json={
            "model": model or settings["model"],
            "messages": messages,
            "temperature": temperature,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def transcribe_audio(data: bytes, *, audio_format: str = "wav", timeout: float = 600.0) -> str:
    """调用云端 ASR，返回纯文本。

    注意：小米这个网关会自己注入转写提示词，请求里不能带 text 部分，否则直接 400。
    """
    settings = config.section("asr")
    response = httpx.post(
        f"{settings['base_url'].rstrip('/')}/chat/completions",
        headers=_headers("asr"),
        json={
            "model": settings["model"],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": base64.b64encode(data).decode(),
                                "format": audio_format,
                            },
                        }
                    ],
                }
            ],
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]
