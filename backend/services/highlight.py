"""高光剪辑：让 LLM 读转写稿，找出「正在介绍商品」的片段。

带货直播里，"高光"就是主播认真介绍商品的那几段——不是笼统地找热闹，
而是识别商品讲解段，再在段内挑出最值得单独剪的一小段（通常落在报价格、逼单处）。
"""
from __future__ import annotations

import json
import re
from typing import List, Optional

from ..models import HighlightClip, ProductSegment, Project, SubtitleCue
from .cloud import chat

PROMPT = """你是一个带货直播切片助手。用户给你一场直播的转写稿，每行一条：

[起始秒-结束秒] 字幕文本

你要找出**所有「主播正在介绍或推销某个商品」的片段**。在带货直播里，
介绍商品的那一段就是我们要的「高光」；闲聊、谢礼物、过场都不算。

对每个片段输出一个对象，字段如下：
- name：商品名。从话术里推断（如"这款保湿面霜"→"保湿面霜"），实在没有就给一个概括词。
- start / end：这段商品讲解的起止秒，**必须使用稿子里出现过的秒数**。
- highlight_start / highlight_end：这段里最值得单独剪成竖屏短视频的一小段，
  建议 20~90 秒，通常落在"报价格 / 讲核心卖点 / 逼单"的地方。
  必须满足 start ≤ highlight_start < highlight_end ≤ end。
- title：一句能让人点进来的标题，不超过 20 字。
- reason：为什么选这段，不超过 15 字，例如"报价格+最后10件"。

硬性要求：
- 只输出一个 JSON 数组，不要 markdown 代码块，不要任何解释文字。
- 整场都没有介绍商品的话，输出 []。"""


def build_transcript(cues: List[SubtitleCue]) -> str:
    """把字幕整理成 LLM 好读、且带时间锚点的格式。

    时间锚点很关键：模型只能引用稿子里出现过的秒数，这样它编不出不存在的时间。
    """
    return "\n".join(f"[{cue.start:.1f}-{cue.end:.1f}] {cue.text}" for cue in cues)


def _extract_json(reply: str) -> list:
    text = reply.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < 0:
        raise RuntimeError(f"模型没返回 JSON 数组：{text[:120]}")
    return json.loads(text[start : end + 1])


def _number(value, low: float, high: float, fallback: Optional[float]) -> Optional[float]:
    """把模型给的数收进 [low, high]。模型偶尔会越界或给字符串，所以必须夹紧。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return min(high, max(low, number))


def find_highlights(project: Project, limit: int = 3) -> List[ProductSegment]:
    """分析转写稿，返回商品讲解段（每段带一个精选窗口）。

    limit 是「想要几段」，0 表示不限。段数靠提示词约束，模型仍可能多给，
    所以下面还会按 limit 截一刀，不能只信它听话。
    """
    if not project.subtitles:
        raise RuntimeError("还没有字幕，先点「转写」")
    if project.duration <= 0:
        raise RuntimeError("视频时长未知，没法定位时间")

    prompt = PROMPT
    if limit > 0:
        prompt += (
            f"\n\n这次最多输出 {limit} 段，按「最值得单独剪成短视频」的程度从高到低排列。"
            f"整场凑不满 {limit} 段就只输出实际有的，不要硬凑。只输出一个 JSON 数组。"
        )

    reply = chat(
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": build_transcript(project.subtitles)},
        ],
        temperature=0.2,
        timeout=600.0,
    )

    segments: List[ProductSegment] = []
    for index, item in enumerate(_extract_json(reply), start=1):
        if not isinstance(item, dict):
            continue

        start = _number(item.get("start"), 0.0, project.duration, None)
        end = _number(item.get("end"), 0.0, project.duration, None)
        if start is None or end is None or end - start < 1.0:
            continue

        highlight_start = _number(item.get("highlight_start"), start, end, start)
        highlight_end = _number(item.get("highlight_end"), highlight_start, end, end)
        if highlight_end - highlight_start < 1.0:
            highlight_start, highlight_end = start, end

        name = str(item.get("name") or f"商品{index}").strip()
        segments.append(
            ProductSegment(
                id=f"seg{index}",
                name=name,
                start=start,
                end=end,
                clips=[
                    HighlightClip(
                        id=f"hl{index}",
                        start=highlight_start,
                        end=highlight_end,
                        title=str(item.get("title") or name).strip(),
                        reason=str(item.get("reason") or "").strip(),
                    )
                ],
            )
        )
    return segments[:limit] if limit > 0 else segments
