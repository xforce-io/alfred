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
    def __init__(
        self,
        base_url: str,
        *,
        client: Optional[httpx.AsyncClient] = None,
        sync_client: Optional[httpx.Client] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client  # injected for tests; None → one client per turn
        self._sync_client = sync_client  # injected for tests; None → one client per call

    @staticmethod
    def _new_client() -> httpx.AsyncClient:
        # 连本地 sidecar 走回环,绝不能经系统代理(http_proxy 会把 127.0.0.1
        # 也代理掉 → /chat 502,e2e 实测踩到)。故 trust_env=False。
        return httpx.AsyncClient(timeout=None, trust_env=False)

    @staticmethod
    def _new_sync_client() -> httpx.Client:
        return httpx.Client(timeout=None, trust_env=False)

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

    # -- 状态查询:milkie 用 AgentResult.status;handle 暂不缓存,默认 False。
    #    完整实现需 serve 暴露运行态查询(待 milkie 扩展)。
    def is_paused(self, agent: Any) -> bool:
        return False

    def is_error(self, agent: Any) -> bool:
        return False

    def is_user_interrupt_paused(self, agent: Any) -> bool:
        return False

    def ensure_chat_compatibility(self) -> bool:
        return False  # milkie 无 dolphin 的 EXPLORE_BLOCK_V2 flag

    # -- milkie 自带机制,no-op --
    def init_trajectory(self, agent: Any, path: str, overwrite: bool = False) -> None:
        pass  # milkie 自带 event sourcing,无需外部 trajectory

    def finalize_trajectory_on_error(self, agent: Any) -> None:
        pass  # 同上

    def set_session_id(self, agent: Any, session_id: str) -> None:
        pass  # milkie 会话身份即 handle.context_id

    def has_skill(self, agent: Any, name: str) -> bool:
        return False  # Python skill 待 milkie#87

    # -- 需 milkie serve 扩展,明确未实现(避免静默错误) --
    def set_variable(self, agent: Any, key: str, value: Any) -> None:
        # 经 milkie serve 的 /context/set 端点跨进程写会话变量(milkie#83 HTTP 暴露)。
        client = self._sync_client or self._new_sync_client()
        owns = self._sync_client is None
        try:
            client.post(
                f"{agent.base_url}/context/set",
                json={"contextId": agent.context_id, "name": key, "value": value},
            )
        finally:
            if owns:
                client.close()

    def get_variable(self, agent: Any, key: str) -> Any:
        client = self._sync_client or self._new_sync_client()
        owns = self._sync_client is None
        try:
            resp = client.post(
                f"{agent.base_url}/context/get",
                json={"contextId": agent.context_id, "name": key},
            )
            return resp.json().get("value")
        finally:
            if owns:
                client.close()

    def register_skillkit(self, agent: Any, skillkit: Any) -> None:
        raise NotImplementedError(
            "MilkieProvider.register_skillkit 需 milkie#87 跨语言工具桥;见 goal.md D"
        )

    def export_session(self, agent: Any) -> dict:
        raise NotImplementedError(
            "MilkieProvider.export_session 走 milkie#128 /session/history;Phase B 实现"
        )

    async def call_llm(
        self,
        context: Any,
        prompt: str,
        temperature: float = 0.3,
        fast: bool = False,
        raise_on_error: bool = True,
    ) -> str:
        # 一次性 LLM 经 serve /llm 端点(milkie#124/#126);无状态,不需 contextId。
        client = self._client or self._new_client()
        owns = self._client is None
        try:
            resp = await client.post(
                f"{self._base_url}/llm",
                json={
                    "messages": [
                        {"role": "user", "content": [{"type": "text", "text": prompt}]}
                    ],
                    "tier": "fast" if fast else "default",
                    "temperature": temperature,
                },
            )
            if resp.status_code != 200:
                # serve 把 gateway 异常映射成 4xx/5xx + {error}。dolphin 语义:
                # raise_on_error=True(memory)→ 抛;False(compressor)→ 错误串当结果。
                err = (resp.json().get("error") if resp.headers.get(
                    "content-type", "").startswith("application/json") else None
                ) or resp.text or f"HTTP {resp.status_code}"
                if raise_on_error:
                    raise RuntimeError(f"LLM call failed: {err}")
                return err
            return (resp.json().get("output") or "").strip()
        finally:
            if owns:
                await client.aclose()
