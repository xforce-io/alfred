"""MilkieProvider — 跨进程驱动 milkie serve 的 AgentProvider 实现。

``run_turn(handle, message, ...)`` 对一条对话发 ``POST /chat``(响应体即 SSE),
用 :class:`SSEParser` 增量解析,经 :func:`milkie_event_to_progress` 适配成 dolphin
``{"_progress": [...]}`` 事件流 —— 与 DolphinProvider 同一中立契约,turn_orchestrator
在其上套 policy。

垂直切片范围:纯文本对话路径。`system_prompt` 暂未透传到 serve(milkie agent 的
prompt 由 agent.md 决定;serve 接 system_prompt override 待后续,见 milkie#82/#86)。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

import httpx

from .adapter import milkie_event_to_progress
from .sse import SSEParser


@dataclass
class MilkieAgentHandle:
    """A milkie conversation handle: which sidecar + which session(contextId)."""

    base_url: str
    context_id: str


class MilkieProvider:
    def __init__(self, base_url: str, *, client: Optional[httpx.AsyncClient] = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client  # injected for tests; None → one client per turn

    @staticmethod
    def _new_client() -> httpx.AsyncClient:
        # 连本地 sidecar 走回环,绝不能经系统代理(http_proxy 会把 127.0.0.1
        # 也代理掉 → /chat 502,e2e 实测踩到)。故 trust_env=False。
        return httpx.AsyncClient(timeout=None, trust_env=False)

    async def create_agent(
        self,
        agent_name: str,
        workspace_path: Any,
        *,
        model_name: Optional[str] = None,
        extra_variables: Optional[dict] = None,
        tools_override: Optional[list] = None,
    ) -> MilkieAgentHandle:
        # milkie agent 由 serve --agent 决定;此处只分配一个会话句柄(contextId)。
        return MilkieAgentHandle(
            base_url=self._base_url,
            context_id=f"{agent_name}-{uuid.uuid4().hex[:8]}",
        )

    async def run_turn(
        self,
        agent: Any,
        message: Any,
        *,
        system_prompt: str = "",
        is_first_turn: bool = False,
        stream_mode: str = "delta",
    ) -> AsyncIterator[dict]:
        handle: MilkieAgentHandle = agent
        client = self._client or self._new_client()
        owns_client = self._client is None
        parser = SSEParser()
        text = message if isinstance(message, str) else str(message)
        payload = {"contextId": handle.context_id, "input": text, "goal": text}
        try:
            async with client.stream("POST", f"{handle.base_url}/chat", json=payload) as resp:
                async for chunk in resp.aiter_text():
                    for event, data_str in parser.feed(chunk):
                        item = milkie_event_to_progress(event, json.loads(data_str))
                        if item is not None:
                            yield {"_progress": [item]}
        finally:
            if owns_client:
                await client.aclose()
