"""Map milkie native SSE events onto dolphin ``_progress`` items(统一中立契约).

A1 把 ``_progress`` 定为 provider 中立契约:turn_orchestrator 消费 ``_progress``
并套 policy 产出 ``TurnEvent``。MilkieProvider 因此把 milkie 事件适配成
``_progress`` item(而非直接产 TurnEvent),从而复用 turn_orchestrator 的全部
policy(循环检测 / 预算 / 失败熔断 …)。

pid 合成:工具块用 milkie 的 ``toolCallId``(running/completed 配对);LLM 块
milkie 无块级 id,固定用 ``"llm"`` —— turn_orchestrator 的 llm 分支不读 pid,
其 fingerprint 含 delta 内容,不会误去重不同 token。

终态(``agent.run.completed`` / ``error`` 帧 / 起止 / 未知)返回 None:turn 的
结束由 SSE 流自然结束表示(对齐 dolphin continue_chat 流结束即 turn 完成)。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional


def milkie_event_to_progress(event: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if event == "message_delta":
        return {"stage": "llm", "delta": data.get("text") or "", "answer": "", "id": "llm"}

    if event == "tool.requested":
        args = data.get("input")
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False) if args is not None else ""
        return {
            "stage": "skill",
            "status": "running",
            "id": data.get("toolCallId") or "",
            "skill_info": {"name": data.get("toolName") or "", "args": args},
        }

    if event == "tool.responded":
        ok = data.get("status") == "ok"
        out = data.get("output") if ok else (data.get("error") or "")
        if not isinstance(out, str):
            out = json.dumps(out, ensure_ascii=False)
        return {
            "stage": "skill",
            "status": "completed" if ok else "failed",
            "answer": out,
            "id": data.get("toolCallId") or "",
            "skill_info": {"name": data.get("toolName") or ""},
        }

    return None
