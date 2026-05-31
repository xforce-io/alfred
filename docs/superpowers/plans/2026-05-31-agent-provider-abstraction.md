# AgentProvider 抽象层 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 alfred 主干代码中所有 `import dolphin` 收敛到一个 `core/agent/provider/dolphin/` 包后面，引入中立的 `AgentProvider` 端口，行为零变化。

**Architecture:** 新增 `provider` 包：`base.py` 定义中立端口/类型/常量（不 import dolphin），`provider/dolphin/` 是唯一实现（import dolphin）。6 个主干消费方改为经 provider 端口访问 dolphin 能力。裸 agent 对象继续流转，不引入 Session 包装。

**Tech Stack:** Python 3.11, pytest, typing.Protocol, dolphin SDK（被封装）。

**测试运行约定：** `.venv/bin/python -m pytest <path> -q`。

---

## File Structure

新建：
- `src/everbot/core/agent/provider/__init__.py` — 公共出口：`get_provider()` + re-export 端口/常量/Skillkit 基类
- `src/everbot/core/agent/provider/base.py` — `AgentProvider` Protocol + 中立常量（不 import dolphin）
- `src/everbot/core/agent/provider/dolphin/__init__.py`
- `src/everbot/core/agent/provider/dolphin/provider.py` — `DolphinProvider`
- `src/everbot/core/agent/provider/dolphin/state.py` — AgentState/PauseType helper
- `src/everbot/core/agent/provider/dolphin/llm.py` — 一次性 LLM 调用
- `src/everbot/core/agent/provider/dolphin/compat.py` — flags + 常量（迁自 infra/dolphin_compat）
- `src/everbot/core/agent/provider/dolphin/factory.py` — 迁自 core/agent/factory.py
- `src/everbot/core/agent/provider/dolphin/skillkit.py` — re-export Skillkit/SkillFunction

测试：
- `tests/unit/test_agent_provider_boundary.py` — 边界守护（grep import dolphin）
- `tests/unit/test_agent_provider_state.py` — 状态 helper 契约
- `tests/unit/test_agent_provider_llm.py` — call_llm 行为
- `tests/unit/test_agent_provider_constants.py` — 常量等价

修改（re-export shim / 走 provider）：
- `core/agent/factory.py`、`infra/dolphin_compat.py`（shim）
- `core/channel/core_service.py`、`web/services/chat_service.py`（状态 helper）
- `core/memory/_extractor_helpers.py`、`core/session/compressor.py`（call_llm）
- `channels/telegram_skillkit.py`（SkillkitBase）

---

## Task 1: provider 包骨架 + 中立端口

**Files:**
- Create: `src/everbot/core/agent/provider/base.py`
- Create: `src/everbot/core/agent/provider/__init__.py`
- Create: `src/everbot/core/agent/provider/dolphin/__init__.py`

- [ ] **Step 1: 写 base.py（中立端口 + 常量）**

```python
"""Neutral AgentProvider port. MUST NOT import dolphin."""
from __future__ import annotations
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

# Neutral constants (values identical to dolphin's current values).
KEY_HISTORY: str = "history"
KEY_HISTORY_COMPACT_ON_PERSIST: str = "_history_compact_on_persist"
KEY_HISTORY_COMPACT_RECENT_TURNS: str = "_history_compact_recent_turns"


@runtime_checkable
class AgentProvider(Protocol):
    async def create_agent(
        self, agent_name: str, workspace_path: Path,
        model_name: Optional[str] = None,
        extra_variables: Optional[dict] = None,
        tools_override: Optional[list[str]] = None,
    ) -> Any: ...

    def is_paused(self, agent: Any) -> bool: ...
    def is_error(self, agent: Any) -> bool: ...
    def is_user_interrupt_paused(self, agent: Any) -> bool: ...

    async def call_llm(
        self, context: Any, prompt: str,
        temperature: float = 0.3, fast: bool = False,
    ) -> str: ...

    def ensure_chat_compatibility(self) -> bool: ...
```

- [ ] **Step 2: 写 provider/dolphin/__init__.py 占位**

```python
```
(空文件)

- [ ] **Step 3: 写 provider/__init__.py（暂只导出端口与常量，get_provider 留到 Task 6）**

```python
from .base import (
    AgentProvider,
    KEY_HISTORY,
    KEY_HISTORY_COMPACT_ON_PERSIST,
    KEY_HISTORY_COMPACT_RECENT_TURNS,
)

__all__ = [
    "AgentProvider",
    "KEY_HISTORY",
    "KEY_HISTORY_COMPACT_ON_PERSIST",
    "KEY_HISTORY_COMPACT_RECENT_TURNS",
]
```

- [ ] **Step 4: 验证可 import**

Run: `.venv/bin/python -c "from src.everbot.core.agent.provider import AgentProvider, KEY_HISTORY; print(KEY_HISTORY)"`
Expected: 打印 `history`

- [ ] **Step 5: Commit**

```bash
git add src/everbot/core/agent/provider/
git commit -m "feat(provider): 新增 AgentProvider 中立端口骨架 (#32)"
```

---

## Task 2: 常量等价测试 + 验证

**Files:**
- Test: `tests/unit/test_agent_provider_constants.py`

- [ ] **Step 1: 写测试**

```python
"""provider 中立常量值必须与 dolphin 当前值一致。"""
from src.everbot.core.agent.provider import (
    KEY_HISTORY,
    KEY_HISTORY_COMPACT_ON_PERSIST,
    KEY_HISTORY_COMPACT_RECENT_TURNS,
)


def test_key_history_matches_dolphin_compat():
    from src.everbot.infra.dolphin_compat import KEY_HISTORY as DC_KEY_HISTORY
    assert KEY_HISTORY == DC_KEY_HISTORY


def test_compact_constants_match_dolphin_compat():
    from src.everbot.infra import dolphin_compat as dc
    assert KEY_HISTORY_COMPACT_ON_PERSIST == dc.KEY_HISTORY_COMPACT_ON_PERSIST
    assert KEY_HISTORY_COMPACT_RECENT_TURNS == dc.KEY_HISTORY_COMPACT_RECENT_TURNS
```

- [ ] **Step 2: 运行测试**

Run: `.venv/bin/python -m pytest tests/unit/test_agent_provider_constants.py -q`
Expected: PASS（2 passed）

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_agent_provider_constants.py
git commit -m "test(provider): 常量等价测试 (#32)"
```

---

## Task 3: dolphin compat 迁移（compat.py + shim）

**Files:**
- Create: `src/everbot/core/agent/provider/dolphin/compat.py`
- Modify: `src/everbot/infra/dolphin_compat.py`（改 shim，保留 `flags` 可被 patch）

- [ ] **Step 1: 写 provider/dolphin/compat.py（迁入 dolphin 部分）**

内容与现 `infra/dolphin_compat.py` 完全相同（`from dolphin.core import flags` + 三个常量 try/except import + `ensure_continue_chat_compatibility`）。直接复制现文件内容。

- [ ] **Step 2: 把 infra/dolphin_compat.py 改为 shim**

保留 `flags` 引用（`test_dolphin_compat.py` 用 `@patch("src.everbot.infra.dolphin_compat.flags")`），并 re-export 常量。为确保 patch 仍生效，`ensure_continue_chat_compatibility` 的实现**保留在本文件**（继续直接用本模块的 `flags`），常量则从 provider 包重新导出：

```python
"""Compatibility shim. Dolphin-facing helpers now live in
``core.agent.provider.dolphin.compat``; this module re-exports them so existing
import paths keep working.  ``flags`` stays importable here because
test_dolphin_compat.py patches ``src.everbot.infra.dolphin_compat.flags``.
"""
from dolphin.core import flags

from ..core.agent.provider.base import (
    KEY_HISTORY,
    KEY_HISTORY_COMPACT_ON_PERSIST,
    KEY_HISTORY_COMPACT_RECENT_TURNS,
)

__all__ = [
    "flags",
    "KEY_HISTORY",
    "KEY_HISTORY_COMPACT_ON_PERSIST",
    "KEY_HISTORY_COMPACT_RECENT_TURNS",
    "ensure_continue_chat_compatibility",
]


def ensure_continue_chat_compatibility() -> bool:
    if flags.is_enabled(flags.EXPLORE_BLOCK_V2):
        flags.set_flag(flags.EXPLORE_BLOCK_V2, False)
        return True
    return False
```

> 注：本文件仍 `import dolphin`（flags），但它是 infra 兼容层、本就是适配 dolphin 的薄垫片。边界守护测试（Task 5）将 `infra/dolphin_compat.py` 与 `provider/dolphin/**` 一并列入白名单——它是 provider 抽象的等价兼容层。`provider/dolphin/compat.py` 是它的规范归属，供 provider 内部使用。

- [ ] **Step 3: 运行受影响测试**

Run: `.venv/bin/python -m pytest tests/unit/test_dolphin_compat.py tests/unit/test_skill_change_detector.py tests/unit/test_agent_provider_constants.py -q`
Expected: PASS（全绿）

- [ ] **Step 4: Commit**

```bash
git add src/everbot/core/agent/provider/dolphin/compat.py src/everbot/infra/dolphin_compat.py
git commit -m "refactor(provider): dolphin compat 迁入 provider 包, infra 改 shim (#32)"
```

---

## Task 4: 状态 helper（state.py）+ 契约测试

**Files:**
- Create: `src/everbot/core/agent/provider/dolphin/state.py`
- Test: `tests/unit/test_agent_provider_state.py`

- [ ] **Step 1: 写失败测试（用 fake agent 模拟 dolphin agent 语义）**

```python
"""DolphinProvider 状态 helper 必须与 AgentState/PauseType 直接比较等价。"""
import pytest
from dolphin.core.agent.agent_state import AgentState, PauseType
from src.everbot.core.agent.provider.dolphin.state import (
    is_paused, is_error, is_user_interrupt_paused,
)


class _FakeAgent:
    def __init__(self, state, pause_type=None):
        self.state = state
        self._pause_type = pause_type


def test_is_paused_true_only_when_paused():
    assert is_paused(_FakeAgent(AgentState.PAUSED)) is True
    assert is_paused(_FakeAgent(AgentState.ERROR)) is False


def test_is_error_true_only_when_error():
    assert is_error(_FakeAgent(AgentState.ERROR)) is True
    assert is_error(_FakeAgent(AgentState.PAUSED)) is False


def test_is_user_interrupt_paused_requires_both():
    assert is_user_interrupt_paused(
        _FakeAgent(AgentState.PAUSED, PauseType.USER_INTERRUPT)) is True
    assert is_user_interrupt_paused(
        _FakeAgent(AgentState.PAUSED, None)) is False
    assert is_user_interrupt_paused(
        _FakeAgent(AgentState.ERROR, PauseType.USER_INTERRUPT)) is False
```

- [ ] **Step 2: 运行验证失败**

Run: `.venv/bin/python -m pytest tests/unit/test_agent_provider_state.py -q`
Expected: FAIL（module/函数不存在）

- [ ] **Step 3: 写 state.py 实现**

```python
"""Dolphin agent 状态判断 helper（封装 AgentState/PauseType）。"""
from typing import Any

from dolphin.core.agent.agent_state import AgentState, PauseType


def is_paused(agent: Any) -> bool:
    return getattr(agent, "state", None) == AgentState.PAUSED


def is_error(agent: Any) -> bool:
    return getattr(agent, "state", None) == AgentState.ERROR


def is_user_interrupt_paused(agent: Any) -> bool:
    return (
        getattr(agent, "state", None) == AgentState.PAUSED
        and getattr(agent, "_pause_type", None) == PauseType.USER_INTERRUPT
    )
```

- [ ] **Step 4: 运行验证通过**

Run: `.venv/bin/python -m pytest tests/unit/test_agent_provider_state.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/everbot/core/agent/provider/dolphin/state.py tests/unit/test_agent_provider_state.py
git commit -m "feat(provider): dolphin 状态 helper + 契约测试 (#32)"
```

---

## Task 5: 一次性 LLM 调用（llm.py）+ 行为测试

注意：现有两处调用模型选择不同——`_extractor_helpers` 用 `default_model or fast_llm or "deepseek-chat"`；`compressor` 用 `fast_llm or "qwen-turbo"`。统一为 `call_llm(context, prompt, temperature, fast)`：`fast=False` 走 extractor 语义，`fast=True` 走 compressor 语义。

**Files:**
- Create: `src/everbot/core/agent/provider/dolphin/llm.py`
- Test: `tests/unit/test_agent_provider_llm.py`

- [ ] **Step 1: 写失败测试**

```python
"""DolphinProvider.call_llm 行为：消息构造、模型选择、错误前缀。"""
import pytest
from unittest.mock import patch, MagicMock


class _FakeConfig:
    def __init__(self, default_model=None, fast_llm=None):
        self.default_model = default_model
        self.fast_llm = fast_llm


class _FakeContext:
    def __init__(self, config):
        self._config = config
    def get_config(self):
        return self._config


def _mk_stream(chunks):
    async def _gen(*a, **k):
        for c in chunks:
            yield c
    return _gen


@pytest.mark.asyncio
async def test_call_llm_returns_stripped_content():
    from src.everbot.core.agent.provider.dolphin import llm as mod
    fake_client = MagicMock()
    fake_client.mf_chat_stream = _mk_stream([{"content": "  hello  "}])
    with patch.object(mod, "LLMClient", return_value=fake_client):
        out = await mod.call_llm(_FakeContext(_FakeConfig(default_model="m1")), "hi")
    assert out == "hello"


@pytest.mark.asyncio
async def test_call_llm_fast_uses_fast_llm_then_qwen_default():
    from src.everbot.core.agent.provider.dolphin import llm as mod
    captured = {}
    def _capture(*a, **k):
        captured["model"] = k.get("model")
        async def _gen():
            yield {"content": "x"}
        return _gen()
    fake_client = MagicMock()
    fake_client.mf_chat_stream = _capture
    with patch.object(mod, "LLMClient", return_value=fake_client):
        await mod.call_llm(_FakeContext(_FakeConfig(fast_llm="ft")), "p", fast=True)
    assert captured["model"] == "ft"


@pytest.mark.asyncio
async def test_call_llm_raises_on_error_prefix():
    from src.everbot.core.agent.provider.dolphin import llm as mod
    fake_client = MagicMock()
    fake_client.mf_chat_stream = _mk_stream([{"content": "❌ boom"}])
    with patch.object(mod, "LLMClient", return_value=fake_client):
        with pytest.raises(RuntimeError):
            await mod.call_llm(_FakeContext(_FakeConfig(default_model="m1")), "hi")
```

- [ ] **Step 2: 运行验证失败**

Run: `.venv/bin/python -m pytest tests/unit/test_agent_provider_llm.py -q`
Expected: FAIL（module 不存在）

- [ ] **Step 3: 写 llm.py（合并两处语义，import 提到模块级便于 patch）**

```python
"""Dolphin 一次性 LLM 调用封装（迁自 _extractor_helpers / compressor）。"""
from typing import Any

from dolphin.core.llm.llm_client import LLMClient
from dolphin.core.common.enums import Messages as DolphinMessages, MessageRole


async def call_llm(
    context: Any, prompt: str, temperature: float = 0.3, fast: bool = False,
) -> str:
    """Single user-message LLM call via dolphin's LLMClient.

    fast=False: model = default_model or fast_llm or "deepseek-chat" (extractor 语义)
    fast=True:  model = fast_llm or "qwen-turbo" (compressor 语义)
    Raises RuntimeError if dolphin surfaced an error string as content.
    """
    llm_client = LLMClient(context)
    msgs = DolphinMessages()
    msgs.append_message(MessageRole.USER, prompt)

    config = context.get_config()
    if fast:
        model = getattr(config, "fast_llm", None) or "qwen-turbo"
    else:
        model = (
            getattr(config, "default_model", None)
            or getattr(config, "fast_llm", None)
            or "deepseek-chat"
        )

    result = ""
    async for chunk in llm_client.mf_chat_stream(
        messages=msgs, model=model, temperature=temperature, no_cache=True,
    ):
        result = chunk.get("content") or ""

    stripped = result.strip()
    if stripped.startswith("❌") or stripped.startswith("failed to call LLM"):
        raise RuntimeError(f"LLM call returned error: {stripped[:120]}")
    return stripped
```

- [ ] **Step 4: 运行验证通过**

Run: `.venv/bin/python -m pytest tests/unit/test_agent_provider_llm.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
git add src/everbot/core/agent/provider/dolphin/llm.py tests/unit/test_agent_provider_llm.py
git commit -m "feat(provider): dolphin 一次性 LLM 调用封装 + 行为测试 (#32)"
```

---

## Task 6: skillkit re-export + DolphinProvider + get_provider

**Files:**
- Create: `src/everbot/core/agent/provider/dolphin/skillkit.py`
- Create: `src/everbot/core/agent/provider/dolphin/provider.py`
- Modify: `src/everbot/core/agent/provider/__init__.py`

- [ ] **Step 1: 写 skillkit.py（re-export 基类）**

```python
"""Re-export dolphin Skillkit/SkillFunction base classes."""
from dolphin.core.skill.skillkit import Skillkit as SkillkitBase
from dolphin.core.skill.skill_function import SkillFunction

__all__ = ["SkillkitBase", "SkillFunction"]
```

- [ ] **Step 2: 写 provider.py（组合各能力；create_agent 委托 factory）**

```python
"""DolphinProvider — 当前唯一的 AgentProvider 实现。"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Optional

from . import state as _state
from . import llm as _llm
from .compat import ensure_continue_chat_compatibility


class DolphinProvider:
    async def create_agent(
        self, agent_name: str, workspace_path: Path,
        model_name: Optional[str] = None,
        extra_variables: Optional[dict] = None,
        tools_override: Optional[list[str]] = None,
    ) -> Any:
        from .factory import get_agent_factory
        return await get_agent_factory().create_agent(
            agent_name, workspace_path,
            model_name=model_name,
            extra_variables=extra_variables,
            tools_override=tools_override,
        )

    def is_paused(self, agent: Any) -> bool:
        return _state.is_paused(agent)

    def is_error(self, agent: Any) -> bool:
        return _state.is_error(agent)

    def is_user_interrupt_paused(self, agent: Any) -> bool:
        return _state.is_user_interrupt_paused(agent)

    async def call_llm(
        self, context: Any, prompt: str,
        temperature: float = 0.3, fast: bool = False,
    ) -> str:
        return await _llm.call_llm(context, prompt, temperature=temperature, fast=fast)

    def ensure_chat_compatibility(self) -> bool:
        return ensure_continue_chat_compatibility()
```

- [ ] **Step 3: 更新 provider/__init__.py 加 get_provider + skillkit re-export**

```python
from .base import (
    AgentProvider,
    KEY_HISTORY,
    KEY_HISTORY_COMPACT_ON_PERSIST,
    KEY_HISTORY_COMPACT_RECENT_TURNS,
)

_provider_singleton: "AgentProvider | None" = None


def get_provider() -> AgentProvider:
    """Return the active AgentProvider (currently the only one: DolphinProvider)."""
    global _provider_singleton
    if _provider_singleton is None:
        from .dolphin.provider import DolphinProvider
        _provider_singleton = DolphinProvider()
    return _provider_singleton


def __getattr__(name):
    # Lazy re-export of dolphin-backed Skillkit base classes so importing the
    # neutral package does not eagerly import dolphin.
    if name in ("SkillkitBase", "SkillFunction"):
        from .dolphin.skillkit import SkillkitBase, SkillFunction
        return {"SkillkitBase": SkillkitBase, "SkillFunction": SkillFunction}[name]
    raise AttributeError(name)


__all__ = [
    "AgentProvider", "get_provider",
    "KEY_HISTORY", "KEY_HISTORY_COMPACT_ON_PERSIST",
    "KEY_HISTORY_COMPACT_RECENT_TURNS",
    "SkillkitBase", "SkillFunction",
]
```

- [ ] **Step 4: 验证 import + 运行时检查 Protocol**

Run: `.venv/bin/python -c "from src.everbot.core.agent.provider import get_provider, AgentProvider; p=get_provider(); print(isinstance(p, AgentProvider))"`
Expected: 打印 `True`

- [ ] **Step 5: Commit**

```bash
git add src/everbot/core/agent/provider/
git commit -m "feat(provider): DolphinProvider 实现 + get_provider + skillkit re-export (#32)"
```

---

## Task 7: 迁移 factory.py 到 provider/dolphin（git mv + shim）

**Files:**
- Move: `src/everbot/core/agent/factory.py` → `src/everbot/core/agent/provider/dolphin/factory.py`
- Create (shim): `src/everbot/core/agent/factory.py`

- [ ] **Step 1: git mv 保留历史**

```bash
git mv src/everbot/core/agent/factory.py src/everbot/core/agent/provider/dolphin/factory.py
```

- [ ] **Step 2: 修正迁移后文件内的相对 import**

`provider/dolphin/factory.py` 原有相对 import 需按新深度调整。原文件在 `core/agent/`，新位置在 `core/agent/provider/dolphin/`（深两层）。把 `from ...infra.dolphin_compat import (...)` 改为 `from .compat import (KEY_HISTORY_COMPACT_ON_PERSIST, KEY_HISTORY_COMPACT_RECENT_TURNS)`（按原导入的符号）。其它 `from ..` / `from ...` 相对 import 逐一加深两级（`..` → `....`，`...` → `.....`）。

> 执行细则：先 `grep -nE "^from \.|^from \.\.|import" provider/dolphin/factory.py` 列出全部相对 import，逐条按"深两级"修正。验证以 Step 4 的 import 测试为准。

- [ ] **Step 3: 写 factory.py shim（保留全部旧导入路径与静态方法）**

```python
"""Backward-compat shim. The real AgentFactory now lives in
``core.agent.provider.dolphin.factory``.  Existing imports
(``from ...core.agent.factory import AgentFactory/create_agent/get_agent_factory``)
keep working through this re-export.
"""
from .provider.dolphin.factory import (  # noqa: F401
    AgentFactory,
    create_agent,
    get_agent_factory,
)

__all__ = ["AgentFactory", "create_agent", "get_agent_factory"]
```

- [ ] **Step 4: 验证全部旧导入路径可用**

Run:
```bash
.venv/bin/python -c "
from src.everbot.core.agent.factory import AgentFactory, create_agent, get_agent_factory
from src.everbot import AgentFactory as A2, create_agent as c2
from src.everbot.core.agent import AgentFactory as A3
assert hasattr(AgentFactory, '_resolve_agent_model')
assert hasattr(AgentFactory, '_append_runtime_paths')
assert hasattr(AgentFactory, '_get_global_config')
print('factory shim OK')
"
```
Expected: 打印 `factory shim OK`

- [ ] **Step 5: 跑 factory 相关测试**

Run: `.venv/bin/python -m pytest tests/unit/test_agent_factory.py tests/unit/test_extract_runtime_skills.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add -A src/everbot/core/agent/
git commit -m "refactor(provider): factory 迁入 provider/dolphin, 原路径改 shim (#32)"
```

---

## Task 8: 改造 core_service.py 走 provider

**Files:**
- Modify: `src/everbot/core/channel/core_service.py`

- [ ] **Step 1: 删除 dolphin import，改 provider**

把 `from dolphin.core.agent.agent_state import AgentState`（line 21）删除。在 import 区加：
```python
from ..agent.provider import get_provider
```
（`KEY_HISTORY` 与 `ensure_continue_chat_compatibility` 仍从 `...infra.dolphin_compat` 取，shim 已保证可用，无需改）

- [ ] **Step 2: 替换状态比较**

- line ~280 `if agent.state != AgentState.PAUSED:` → `if not get_provider().is_paused(agent):`
- line ~292 `and agent.state != AgentState.PAUSED` → `and not get_provider().is_paused(agent)`
- line ~298 `if agent.state == AgentState.ERROR:` → `if get_provider().is_error(agent):`

（裸 `agent.executor.context` / `agent.snapshot.export_portable_session()` 保留不动）

- [ ] **Step 3: 验证无 dolphin import 残留**

Run: `grep -nE "import dolphin|from dolphin|AgentState" src/everbot/core/channel/core_service.py`
Expected: 无输出

- [ ] **Step 4: 跑相关测试**

Run: `.venv/bin/python -m pytest tests/unit/test_channel_core_service.py tests/unit/test_orphan_tool_repair.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/everbot/core/channel/core_service.py
git commit -m "refactor(provider): core_service 状态判断走 provider (#32)"
```

---

## Task 9: 改造 chat_service.py 走 provider

**Files:**
- Modify: `src/everbot/web/services/chat_service.py`

- [ ] **Step 1: 删 dolphin import，加 provider**

删除 line 17 `from dolphin.core.agent.agent_state import AgentState, PauseType`。加：
```python
from ...core.agent.provider import get_provider
```

- [ ] **Step 2: 替换两处比较（line ~407, ~436）**

`if agent.state == AgentState.PAUSED and agent._pause_type == PauseType.USER_INTERRUPT:`
→ `if get_provider().is_user_interrupt_paused(agent):`
（两处相同替换；裸 `agent.interrupt()` / `agent.resume_with_input()` 保留）

- [ ] **Step 3: 验证无残留**

Run: `grep -nE "import dolphin|from dolphin|AgentState|PauseType" src/everbot/web/services/chat_service.py`
Expected: 无输出

- [ ] **Step 4: 跑相关测试**

Run: `.venv/bin/python -m pytest tests/integration/test_user_intervention.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/everbot/web/services/chat_service.py
git commit -m "refactor(provider): chat_service 打断判断走 provider (#32)"
```

---

## Task 10: 改造 _extractor_helpers.py + compressor.py 走 provider.call_llm

**Files:**
- Modify: `src/everbot/core/memory/_extractor_helpers.py`
- Modify: `src/everbot/core/session/compressor.py`

- [ ] **Step 1: _extractor_helpers.call_dolphin_llm 改为委托 provider**

把 `call_dolphin_llm` 函数体（line 62-97）整体替换为：
```python
async def call_dolphin_llm(context: Any, prompt: str, temperature: float = 0.3) -> str:
    """Call the LLM via the active provider (single user-message prompt)."""
    from ..agent.provider import get_provider
    return await get_provider().call_llm(context, prompt, temperature=temperature, fast=False)
```
（保留函数名与签名，调用方 event_extractor / profile_extractor 无需改）

- [ ] **Step 2: compressor._generate_summary 改为委托 provider**

把 `_generate_summary`（line 168-201）内 dolphin import + LLMClient 调用替换为：
```python
    async def _generate_summary(
        self, old_summary: str, messages: List[Dict[str, Any]]
    ) -> str:
        messages_text = _format_messages_for_prompt(messages)
        if old_summary:
            old_summary_block = f"之前的摘要：\n{old_summary}\n"
        else:
            old_summary_block = ""

        prompt = _SUMMARY_PROMPT_TEMPLATE.format(
            old_summary_block=old_summary_block,
            messages_text=messages_text,
        )

        from ..agent.provider import get_provider
        return await get_provider().call_llm(
            self._context, prompt, temperature=0.3, fast=True,
        )
```

- [ ] **Step 3: 验证无残留**

Run: `grep -nE "import dolphin|from dolphin|LLMClient" src/everbot/core/memory/_extractor_helpers.py src/everbot/core/session/compressor.py`
Expected: 无输出

- [ ] **Step 4: 跑相关测试**

Run: `.venv/bin/python -m pytest tests/unit/test_memory_dedup.py tests/unit/test_self_reflection.py tests/integration/test_channel_session_compression.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/everbot/core/memory/_extractor_helpers.py src/everbot/core/session/compressor.py
git commit -m "refactor(provider): 记忆抽取与会话压缩 LLM 调用走 provider (#32)"
```

---

## Task 11: 改造 telegram_skillkit.py 走 SkillkitBase

**Files:**
- Modify: `src/everbot/channels/telegram_skillkit.py`

- [ ] **Step 1: 替换 import 与基类**

删除 line 16-17：
```python
from dolphin.core.skill.skillkit import Skillkit
from dolphin.core.skill.skill_function import SkillFunction
```
改为：
```python
from ..core.agent.provider import SkillkitBase, SkillFunction
```
并把 `class TelegramSkillkit(Skillkit):`（line 56）改为 `class TelegramSkillkit(SkillkitBase):`。

- [ ] **Step 2: 验证无残留**

Run: `grep -nE "import dolphin|from dolphin" src/everbot/channels/telegram_skillkit.py`
Expected: 无输出

- [ ] **Step 3: import 冒烟**

Run: `.venv/bin/python -c "from src.everbot.channels.telegram_skillkit import TelegramSkillkit; print('tg ok')"`
Expected: 打印 `tg ok`

- [ ] **Step 4: Commit**

```bash
git add src/everbot/channels/telegram_skillkit.py
git commit -m "refactor(provider): telegram skillkit 走 provider 基类 (#32)"
```

---

## Task 12: 边界守护测试（核心验收）

**Files:**
- Test: `tests/unit/test_agent_provider_boundary.py`

- [ ] **Step 1: 写边界守护测试**

```python
"""硬约束：除 provider/dolphin/** 与 infra/dolphin_compat.py 外，
src/everbot 主干代码不得 import dolphin。"""
import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "everbot"
_ALLOW_PREFIXES = (
    str(_SRC / "core" / "agent" / "provider" / "dolphin"),
    str(_SRC / "infra" / "dolphin_compat.py"),
)
_PAT = re.compile(r"^\s*(?:from|import)\s+dolphin(?:\.|\s|$)", re.MULTILINE)


def test_no_dolphin_imports_outside_provider():
    offenders = []
    for py in _SRC.rglob("*.py"):
        sp = str(py)
        if sp.startswith(_ALLOW_PREFIXES):
            continue
        text = py.read_text(encoding="utf-8")
        if _PAT.search(text):
            offenders.append(sp)
    assert not offenders, "Unexpected dolphin imports outside provider:\n" + "\n".join(offenders)
```

- [ ] **Step 2: 运行（应已通过——前序任务已清理）**

Run: `.venv/bin/python -m pytest tests/unit/test_agent_provider_boundary.py -q`
Expected: PASS（1 passed）。若 FAIL，offenders 列表会列出残留文件，回到对应 Task 修复。

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_agent_provider_boundary.py
git commit -m "test(provider): 边界守护——主干不得 import dolphin (#32)"
```

---

## Task 13: 全量回归

- [ ] **Step 1: 跑全部单元测试**

Run: `.venv/bin/python -m pytest tests/unit -q`
Expected: 全绿（与改造前数量一致）

- [ ] **Step 2: 跑集成测试**

Run: `.venv/bin/python -m pytest tests/integration -q`
Expected: 全绿

- [ ] **Step 3: 若有 acceptance_test.sh，跑导入冒烟**

Run: `.venv/bin/python -c "import src.everbot; print('import everbot OK')"`
Expected: 打印 `import everbot OK`

- [ ] **Step 4: 修复任何回归后 commit**

```bash
git add -A && git commit -m "test(provider): 全量回归绿色 (#32)"
```

---

## Task 14: 切换运行服务 + 端到端验证

- [ ] **Step 1: 启动服务并冒烟（用项目既有方式）**

按 QUICKSTART/bin 启动 alfred 服务，确认进程起得来、无 import 错误。

- [ ] **Step 2: 端到端验证清单**

逐项确认行为与改造前一致：
- 普通对话：流式 delta + 工具调用事件正常
- 会话持久化与重连恢复历史
- 用户打断 + resume_with_input
- 心跳（heartbeat）路径

- [ ] **Step 3: 记录验证结果，最终汇报**

把端到端结果汇总，准备 ship/PR。

---

## Self-Review 检查

- **Spec 覆盖**：6 个耦合文件 → Task 8/9/10/11（消费方）+ Task 3/5/7（迁移）；端口 → Task 1；常量 → Task 2；状态 → Task 4；LLM → Task 5；skillkit → Task 6/11；边界守护 → Task 12；回归 → Task 13；e2e → Task 14。✅ 全覆盖。
- **占位符**：无 TBD/TODO，所有代码步骤含完整代码。✅
- **类型一致**：`get_provider()`、`is_paused/is_error/is_user_interrupt_paused`、`call_llm(context, prompt, temperature, fast)`、`SkillkitBase/SkillFunction`、`ensure_chat_compatibility` 在端口（Task 1）、实现（Task 4/5/6）、消费方（Task 8-11）命名一致。✅
- **风险点**：Task 7 相对 import 加深是最易错处，已给 grep+逐条修正指引，并用 import 测试兜底。
