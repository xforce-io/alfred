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
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx

from .adapter import milkie_event_to_progress
from .sse import SSEParser
from .....infra.user_data import get_user_data_manager

logger = logging.getLogger(__name__)


def _resolve_agent_workspace(agent_name: str) -> Path:
    """解析 agent 工作区目录(= ``~/.alfred/agents/<name>``)。

    经 user-data manager 的 ``get_agent_dir`` 取确定路径;独立成函数以便测试
    monkeypatch,无需真实 ~/.alfred。"""
    return get_user_data_manager().get_agent_dir(agent_name)


# 共享 reflector agent 名(#34 C):milkie 丢弃 per-turn system_prompt,故自省不能复用业务
# agent + override(会被业务人设污染),改路由到此独立 agent —— 其 agent.md 的 systemPrompt
# 即 reflect-JSON 提示,池内单例(shutdown 自动回收)、contextId 隔离、上下文自包含在 message。
REFLECTOR_AGENT = "_reflector"


def _default_system_prompt_loader(agent_name: str) -> str:
    """构建 milkie agent 的 system prompt —— 真实来源。

    经 :class:`WorkspaceLoader` 读取并合并 agent 工作区的 SOUL/AGENTS/SKILLS/
    USER/MEMORY.md(同 dolphin agent 的 ``$workspace_instructions``,但不耦合
    dolphin factory)。workspace 不存在即 bug,fail loud(raise),绝不静默返回 ""。
    """
    if agent_name == REFLECTOR_AGENT:
        # reflector 的 systemPrompt 即 reflect-JSON 提示;不读业务 workspace(无污染、无需 workspace)。
        from ....runtime.inspector import _REFLECT_SYSTEM_PROMPT  # 延迟引,避免模块期循环
        return _REFLECT_SYSTEM_PROMPT

    from .....infra.workspace import WorkspaceLoader
    from .skills import build_milkie_skills_section, discover_skills

    workspace = _resolve_agent_workspace(agent_name)
    if not workspace.exists():
        raise FileNotFoundError(
            f"agent workspace not found for '{agent_name}': {workspace}"
        )
    base = WorkspaceLoader(workspace).build_system_prompt()

    # 动态发现 shell 型 skill 并注入(milkie 无 dolphin 的 ResourceSkillkit;agent 经
    # 内建 run_command(milkie#134)读 SKILL.md 并跑脚本 —— 与 dolphin 能力对等)。
    # per-agent allowlist:everbot.agents.<name>.skills.include/exclude(A3,对齐 dolphin)。
    include, exclude = _agent_skill_filter(agent_name)
    section = build_milkie_skills_section(
        discover_skills(workspace, include=include, exclude=exclude), workspace
    )
    if section:
        base = f"{base}\n\n---\n\n{section}" if base else section

    # telegram-serving agent:注入附件输出约定指令(milkie 下文件发送靠 <<<send_file>>>
    # 标记 + alfred channel 投递,见 attachment_directives / #38 telegram 原生化)。
    if _is_telegram_serving(agent_name):
        from .....channels.attachment_directives import ATTACHMENT_INSTRUCTION
        base = f"{base}\n\n---\n\n{ATTACHMENT_INSTRUCTION}" if base else ATTACHMENT_INSTRUCTION
    return base


def _agent_skill_filter(agent_name: str):
    """读 everbot.agents.<name>.skills.{include,exclude} → (include, exclude)。缺省 (None, None)。"""
    try:
        from .....infra.config import get_config

        everbot_cfg = (get_config() or {}).get("everbot", {}) or {}
        agent_cfg = (everbot_cfg.get("agents", {}) or {}).get(agent_name, {}) or {}
        skills_cfg = agent_cfg.get("skills", {}) or {}
        return skills_cfg.get("include"), skills_cfg.get("exclude")
    except Exception:
        return None, None


def _is_telegram_serving(agent_name: str) -> bool:
    """该 agent 是否绑定到某 telegram 频道(决定是否注入附件约定指令)。"""
    try:
        from .....infra.config import get_config
        from .. import _telegram_serving_agents

        everbot_cfg = (get_config() or {}).get("everbot", {}) or {}
        return agent_name in _telegram_serving_agents(everbot_cfg)
    except Exception:
        return False


@dataclass
class MilkieAgentHandle:
    """A milkie conversation handle: which sidecar + which session(contextId)。

    ``name`` 携带 agent 名:trunk(web chat_service / session persistence)以
    ``agent.name`` 取值,milkie handle 必须提供,否则 AttributeError 崩溃。
    默认 ""(置 context_id 之后,保持既有 2-arg 位置构造 base_url/context_id 不破)。
    """

    base_url: str
    context_id: str
    name: str = ""


class MilkieProvider:
    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        client: Optional[httpx.AsyncClient] = None,
        sync_client: Optional[httpx.Client] = None,
        pool: Optional[Any] = None,
        system_prompt_loader: Optional[Any] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") if base_url else None
        self._client = client  # injected for tests; None → one client per turn
        self._sync_client = sync_client  # injected for tests; None → one client per call
        # 惰性:构造不做任何 config/factory I/O。pool 首次实际使用(create_agent)时才装配。
        self._pool = pool
        self._system_prompt_loader = system_prompt_loader or _default_system_prompt_loader

    def _get_pool(self):
        """惰性装配 pool:首次 create_agent 时才读 config + dolphin.yaml + factory。"""
        if self._pool is None:
            self._pool = self._build_pool()
        return self._pool

    def _build_pool(self):
        """装配 launcher + pool:从 alfred config(everbot.milkie)取 sidecar 运行参数,
        从 dolphin global config(llms/clouds/model 档)取模型路由,组成 SidecarPool。"""
        from .launcher import SidecarLauncher
        from .pool import SidecarPool
        from .sidecar import MilkieSidecar
        from .....infra.config import get_config

        cfg = (get_config() or {}).get("everbot", {}) or {}
        milkie_cfg = cfg.get("milkie", {}) or {}
        repo_root = Path(__file__).resolve().parents[6]   # …/alfred
        dist_path = Path(
            milkie_cfg.get("dist_path")
            or (repo_root.parent / "milkie" / "dist" / "cli" / "index.js")
        )
        data_dir_root = Path(milkie_cfg.get("data_dir_root") or "~/.alfred/milkie").expanduser()
        node_bin = milkie_cfg.get("node_bin") or "node"

        # 模型路由读 config/dolphin.yaml(纯 YAML),经 model_config 定位 —— 不再经
        # dolphin factory 的 global_config_path(去 dolphin 耦合,#38)。
        from ..model_config import load_model_config
        mc = load_model_config()

        launcher = SidecarLauncher(
            dist_path=dist_path, data_dir_root=data_dir_root, node_bin=node_bin,
            llms=mc.llms, clouds=mc.clouds,
            default_model=mc.default_model, fast_model=mc.fast_model,
        )
        ready_timeout = float(milkie_cfg.get("ready_timeout", 20.0))

        def _build(agent_name: str):
            # per-agent 模型(everbot.agents.<name>.model > everbot.default_model),否则所有
            # agent 都用全局默认模型(实测 bug:demo_agent 被用成 kimi-code 而非其配置的 volcengine)。
            from ...agent_config import resolve_agent_model
            per_agent_model = resolve_agent_model(agent_name) or None
            spec = launcher.build(
                agent_name,
                system_prompt=self._system_prompt_loader(agent_name),
                default_model=per_agent_model,
            )
            return spec.cmd, spec.env

        return SidecarPool(
            build=_build,
            sidecar_factory=lambda cmd, env: MilkieSidecar(
                cmd, env=env, ready_timeout=ready_timeout
            ),
        )

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
        # 经 pool 惰性 spawn/复用 per-agent 的 milkie serve;handle 携带该 serve 的
        # base_url(动态端口),而非固定 config base_url。
        if tools_override is not None:
            # 已知限制(#38):milkie serve 的 agent 工具由 agent.md 的 FSM state 决定,
            # 暂不支持 per-create 工具限权。heartbeat 的只读工具集限制在 milkie 下未生效
            # (agent 仍持全量工具含 run_command)。待 milkie serve 支持运行时工具限权后落地。
            logger.warning(
                "MilkieProvider.create_agent: tools_override 暂不支持(milkie serve 无运行时工具限权);"
                "agent '%s' 将使用 agent.md 定义的全量工具。", agent_name,
            )
        sidecar = await self._get_pool().get_or_spawn(agent_name)
        return MilkieAgentHandle(
            base_url=sidecar.base_url,
            context_id=f"{agent_name}-{uuid.uuid4().hex[:8]}",
            name=agent_name,
        )

    async def shutdown_sidecars(self) -> None:
        # pool 从未装配 → 什么也没 spawn,no-op(不为关停而强行 _build_pool)。
        if self._pool is not None:
            await self._pool.shutdown_all()

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
                if resp.status_code >= 400:
                    # 非2xx 不能静默吞:不抛 → 无事件 → core_service 显示「(无响应)」。
                    # 读 body 并抛清晰 RuntimeError(headers 此时已就绪,可读 status)。
                    body = await resp.aread()
                    raise RuntimeError(
                        f"milkie /chat failed: HTTP {resp.status_code}: "
                        f"{body.decode('utf-8', 'replace')[:500]}"
                    )
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
            resp = client.post(
                f"{agent.base_url}/context/set",
                json={"contextId": agent.context_id, "name": key, "value": value},
            )
            resp.raise_for_status()  # 非2xx 不能静默吞,明确抛错
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
            resp.raise_for_status()  # 非2xx 不能静默返回 None,明确抛错
            return resp.json().get("value")
        finally:
            if owns:
                client.close()

    def register_skillkit(self, agent: Any, skillkit: Any) -> None:
        # #38 telegram 原生化:不再走"跨语言桥"。telegram 文件/图片发送改由 alfred
        # channel 的输出约定(<<<send_file: ...>>>,见 attachment_directives)在 turn 后
        # 投递 —— 能力不丢、不耦合 milkie。故此处对 milkie agent 是优雅 no-op(不再
        # NotImplementedError 阻断 telegram-serving agent)。其它非约定型 Python skillkit
        # 若将来要在 milkie 下原生可用,另行设计(非本任务)。
        name = getattr(skillkit, "getName", lambda: type(skillkit).__name__)()
        logger.debug(
            "MilkieProvider.register_skillkit no-op for '%s'(milkie 经输出约定提供文件发送)", name
        )

    def export_session(self, agent: Any) -> dict:
        # 全量历史经 serve /session/history(milkie#128)取回 canonical Message[],
        # 翻译成 alfred history 格式。variables 走 serve 自持久化,此处不导(milkie#130)。
        client = self._sync_client or self._new_sync_client()
        owns = self._sync_client is None
        try:
            resp = client.post(
                f"{agent.base_url}/session/history",
                json={"contextId": agent.context_id},
            )
            if resp.status_code == 404:
                # 新会话,serve 尚无该 context → 空历史(不抛)。
                return {"history_messages": [], "variables": {}}
            resp.raise_for_status()
            messages = resp.json().get("messages", [])
        finally:
            if owns:
                client.close()
        return {
            "history_messages": _milkie_messages_to_history(messages),
            "variables": {},
        }

    def needs_history_restore(self) -> bool:
        return False  # serve 用 sqlite/jsonl 自持久化(#130),同 contextId 重启自动恢复

    async def interrupt(self, agent: Any) -> None:
        # 经 serve /interrupt 端点跨进程发中断信号。
        client = self._client or self._new_client()
        owns = self._client is None
        try:
            resp = await client.post(
                f"{agent.base_url}/interrupt",
                json={"contextId": agent.context_id},
            )
            resp.raise_for_status()  # 非2xx 不能静默吞,明确抛错
        finally:
            if owns:
                await client.aclose()

    async def resume(self, agent: Any, message: str) -> None:
        # milkie /resume 是流式(产新一轮 turn 事件);此处把消息注入并消费完整个流
        # (调用方语义只需「续跑」,不消费事件 → 排空即可)。
        handle: MilkieAgentHandle = agent
        client = self._client or self._new_client()
        owns = self._client is None
        try:
            async with client.stream(
                "POST", f"{handle.base_url}/resume",
                json={"contextId": handle.context_id, "input": message},
            ) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise RuntimeError(
                        f"milkie /resume failed: HTTP {resp.status_code}: "
                        f"{body.decode('utf-8', 'replace')[:500]}"
                    )
                async for _ in resp.aiter_text():
                    pass   # 排空流,确保 serve 续跑完成
        finally:
            if owns:
                await client.aclose()

    async def call_llm(
        self,
        context: Any,
        prompt: str,
        temperature: float = 0.3,
        fast: bool = False,
        raise_on_error: bool = True,
    ) -> str:
        if not self._base_url:
            raise RuntimeError(
                "MilkieProvider.call_llm 需要配置的 base_url(everbot.milkie.base_url);"
                "per-agent pool 模式下无固定 serve,call_llm 暂不支持(见 goal.md)"
            )
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


def _milkie_messages_to_history(messages: list) -> list:
    """milkie canonical ``Message[]`` → alfred history 格式(OpenAI 风格)。

    - user/assistant 的 text 内容块拼成字符串 ``content``;
    - assistant 的 ``tool_use`` 块 → ``tool_calls:[{id,type:function,function:{name,arguments}}]``
      (arguments 为 input 的 JSON 串,对齐 dolphin/OpenAI 形态);
    - tool message 的每个 ``tool_result`` → 一条 ``{role:tool,tool_call_id,content}``。
    """
    out: list = []
    for m in messages:
        role = m.get("role")
        content = m.get("content") or []
        if role == "user":
            text = "".join(c.get("text", "") for c in content if c.get("type") == "text")
            out.append({"role": "user", "content": text})
        elif role == "assistant":
            text = "".join(c.get("text", "") for c in content if c.get("type") == "text")
            tool_calls = [
                {
                    "id": c.get("id"),
                    "type": "function",
                    "function": {
                        "name": c.get("name"),
                        "arguments": json.dumps(c.get("input") or {}, ensure_ascii=False),
                    },
                }
                for c in content
                if c.get("type") == "tool_use"
            ]
            msg = {"role": "assistant", "content": text}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
        elif role == "tool":
            for c in content:
                if c.get("type") == "tool_result":
                    out.append({
                        "role": "tool",
                        "tool_call_id": c.get("tool_use_id"),
                        "content": c.get("content", ""),
                    })
    # A4 数据卫生:milkie 历史可能含中断轮留下的空 assistant / orphan tool(无配对
    # tool_use),下游送 LLM 会 400。复用与 dolphin 保存路径同一套纯变换(惰性 import,
    # persistence 顶层不引 milkie → 无循环)。
    from ....session.persistence import SessionPersistence
    out = SessionPersistence._filter_empty_assistant_messages(out)
    out = SessionPersistence._heal_orphan_tool_messages(out)
    return out
