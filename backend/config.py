"""模型接入配置。

所有模型参数都能在工作台的「设置」里改，不必碰环境变量。
配置落在 clipwork/config.json；密钥留空时会从环境变量兜底，省得第一次还要手填。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.json"

# 默认填一个 OpenAI 兼容网关的占位地址。换成你自己的即可，
# 也可以在工作台的「设置」里改，改完会写进 config.json（那个文件不入库）。
DEFAULT_GATEWAY = "https://api.openai.com/v1"

DEFAULTS: Dict[str, Dict[str, Any]] = {
    "llm": {
        "base_url": DEFAULT_GATEWAY,
        "api_key": "",
        "model": "mimo-v2.6-flash",
    },
    "asr": {
        "provider": "cloud",  # cloud = 走 OpenAI 兼容网关；llama = 本地 llama.cpp 跑 Qwen3-ASR
        "base_url": DEFAULT_GATEWAY,
        "api_key": "",
        "model": "mimo-v2.5-asr",
        # 本地走 llama.cpp。下面三项留空就用 vendor/ 下的默认位置
        "llama_bin": "",
        "llama_model": "",
        "llama_mmproj": "",
        "llama_port": 8081,
    },
    "export": {
        # 留空表示导到工程自己的 renders 目录；填了就直接落到这个本地目录
        "dir": "",
    },
    "highlight": {
        # 让模型挑几段商品讲解；0 表示不限，有几段要几段
        "limit": 3,
    },
}


def load() -> Dict[str, Dict[str, Any]]:
    data: Dict[str, Dict[str, Any]] = {name: dict(values) for name, values in DEFAULTS.items()}
    if CONFIG_PATH.exists():
        try:
            stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = {}
        for name, values in DEFAULTS.items():
            merged = dict(values)
            # 只接受认识的字段，避免手工编辑配置时塞进乱七八糟的东西
            merged.update({k: v for k, v in (stored.get(name) or {}).items() if k in values})
            data[name] = merged

    # 第一次运行时从环境变量兜底
    fallback = os.getenv("CLIPWORK_MODEL_API_KEY", "")
    for name in ("llm", "asr"):
        if not data[name]["api_key"]:
            data[name]["api_key"] = fallback
    return data


def save(data: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def section(name: str) -> Dict[str, Any]:
    return load()[name]


def api_key(name: str) -> str:
    key = section(name).get("api_key") or ""
    if not key:
        raise RuntimeError(f"{name} 还没配置密钥，去工作台顶部的「设置」里填一下")
    return key


# 本地转写是串行的；云端可以并发，并发越高越快，但要留着网关限额
ASR_CONCURRENCY = int(os.getenv("CLIPWORK_ASR_CONCURRENCY", "8"))
