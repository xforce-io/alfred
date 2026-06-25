"""#130 T1 — 机械给报告每条信号附 top1 原文链接。

报告型脚本在 stdout 末尾机械输出一个 ``<PROVENANCE>{"signals":[...]}</PROVENANCE>``
块(每条信号 top1 ``{title,url}``)。投递侧从 milkie run 事件里取出该块、渲染成
footer 追加到推送 —— **独立于 LLM 散文**(LLM 可能把链接摘掉)。纯函数,无 IO。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

_BLOCK_RE = re.compile(r"<PROVENANCE>(.*?)</PROVENANCE>", re.DOTALL)


def extract_provenance_block(text: str) -> List[Dict[str, str]]:
    """从一段文本里取出 PROVENANCE 块的 signals(title+url 俱全者)。

    Robust:无块 / 坏 JSON / 缺字段一律降级为 ``[]``,绝不抛 —— 投递路径不能因
    溯源附加而崩。"""
    m = _BLOCK_RE.search(text or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except (ValueError, TypeError):
        return []
    signals = data.get("signals") if isinstance(data, dict) else None
    if not isinstance(signals, list):
        return []
    out: List[Dict[str, str]] = []
    for s in signals:
        if not isinstance(s, dict):
            continue
        title, url = s.get("title"), s.get("url")
        if isinstance(title, str) and title and isinstance(url, str) and url:
            out.append({"title": title, "url": url})
    return out


def extract_signals_from_events(events: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """扫 milkie run 事件,从 ``tool.responded`` 的 ``output.stdout`` 里取 PROVENANCE
    块。报告脚本那步带块;取最后一个带块的 stdout(最终报告)。robust:任何字段缺
    失/类型不符都跳过,绝不抛。"""
    found: List[Dict[str, str]] = []
    for e in events:
        if not isinstance(e, dict) or e.get("type") != "tool.responded":
            continue
        output = (e.get("payload") or {}).get("output")
        stdout = output.get("stdout") if isinstance(output, dict) else None
        if not isinstance(stdout, str):
            continue
        signals = extract_provenance_block(stdout)
        if signals:
            found = signals
    return found


_FOOTER_HEADER = "📎 原文链接（机械附加，未经 LLM 改写）"


def render_provenance_footer(signals: List[Dict[str, str]]) -> str:
    """把 signals 渲染成追加到报告尾部的 footer。空 → 空串(正文不变)。"""
    if not signals:
        return ""
    lines = [f"- {s['title']} — {s['url']}" for s in signals]
    return "\n\n" + _FOOTER_HEADER + "\n" + "\n".join(lines)


def append_provenance_footer(result: str, events: List[Dict[str, Any]]) -> str:
    """报告投递前的机械加工:剥掉 LLM 可能抄进散文的裸 ``<PROVENANCE>`` 机器块(不外泄
    给用户),再从 run 事件取 evidence、渲染干净 footer 追加到 ``result``。无 evidence
    → 仅返回剥块后的正文。独立于 LLM 散文。"""
    cleaned = _BLOCK_RE.sub("", result or "").rstrip()
    footer = render_provenance_footer(extract_signals_from_events(events))
    return cleaned + footer if footer else cleaned
