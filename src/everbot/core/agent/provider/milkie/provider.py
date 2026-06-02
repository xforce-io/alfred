"""MilkieProvider — 跨进程驱动 milkie serve 的最小 provider(垂直切片)。

``run_turn`` 对一条对话发 ``POST /chat``(响应体即 SSE),用 :class:`SSEParser`
增量解析,经 :func:`milkie_event_to_turn_event` 适配成 alfred :class:`TurnEvent`
后逐个 yield。本切片只覆盖纯文本路径;完整的 AgentProvider 接口收敛见
xforce-io/alfred#32。
"""
from __future__ import annotations

import json
from typing import AsyncIterator, Optional

import httpx

from everbot.core.runtime.turn_policy import TurnEvent
from .adapter import milkie_event_to_turn_event
from .sse import SSEParser


class MilkieProvider:
    def __init__(self, base_url: str, *, client: Optional[httpx.AsyncClient] = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client  # injected for tests; None → one client per turn

    @staticmethod
    def _new_client() -> httpx.AsyncClient:
        # 连本地 sidecar 走回环,绝不能经系统代理:http_proxy 会把 127.0.0.1
        # 也代理掉 → /chat 502(e2e 实测踩到)。故 trust_env=False。
        return httpx.AsyncClient(timeout=None, trust_env=False)

    async def run_turn(
        self, input: str, *, context_id: str, goal: str = ""
    ) -> AsyncIterator[TurnEvent]:
        client = self._client or self._new_client()
        owns_client = self._client is None
        parser = SSEParser()
        payload = {"contextId": context_id, "input": input, "goal": goal or input}
        try:
            async with client.stream("POST", f"{self._base_url}/chat", json=payload) as resp:
                async for chunk in resp.aiter_text():
                    for event, data_str in parser.feed(chunk):
                        turn_event = milkie_event_to_turn_event(event, json.loads(data_str))
                        if turn_event is not None:
                            yield turn_event
        finally:
            if owns_client:
                await client.aclose()
