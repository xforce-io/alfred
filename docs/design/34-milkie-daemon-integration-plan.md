# milkie daemon 集成(#3)+ 软翻默认(#4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 alfred daemon 端到端可跑 milkie —— MilkieProvider 自管理 per-agent sidecar 池(惰性 spawn + 常驻),agent 创建路径收敛到 provider,补 resume,daemon 生命周期统一回收 sidecar;并把默认 provider 软翻为 milkie,telegram-serving agent 自动回退 dolphin。

**Architecture:** `get_provider()` 同步返回全局单例;新增 `get_provider_for_agent(name)` 做 per-agent 路由(显式配置 > telegram 自动回退 > 全局)。MilkieProvider 内置 `SidecarLauncher`(配置→serve 命令)+ `SidecarPool`(agent_name→进程,惰性/并发安全/统一关闭)。dolphin 路径行为零变化,全程可切回。

**Tech Stack:** Python 3 / asyncio / httpx / pytest(`asyncio_mode=auto`)/ 现有 `MilkieSidecar`、`agent_spec`、`MilkieProvider`。milkie serve 为 Node 子进程。

**环境前置(每个 Run 命令都需要):**
- `PYTHONPATH=src`(pyproject 的 `pythonpath=["."]` 指不到 src layout)。
- 用 `.venv/bin/python -m pytest`;async 测试无需标记。
- e2e 需 `cd ../milkie && npm run build`;未 build 自动 skip。

**设计事实源:** `docs/design/34-milkie-daemon-integration.md`。

---

## 文件结构

| 文件 | 责任 | 动作 |
|---|---|---|
| `src/everbot/core/agent/provider/milkie/launcher.py` | alfred agent 配置 → `milkie serve` 命令 + env + data-dir(纯函数) | Create |
| `src/everbot/core/agent/provider/milkie/pool.py` | `SidecarPool`:agent_name→MilkieSidecar,惰性/并发安全/统一关闭 | Create |
| `src/everbot/core/agent/provider/milkie/provider.py` | 接 launcher+pool;create_agent 走池;实现 resume;shutdown_sidecars | Modify |
| `src/everbot/core/agent/provider/__init__.py` | 新增 `get_provider_for_agent`;DolphinProvider 加 `shutdown_sidecars` no-op | Modify |
| `src/everbot/core/agent/provider/dolphin/provider.py` | 加 `shutdown_sidecars()` no-op | Modify |
| `src/everbot/core/agent/agent_service.py` | create_agent_instance 走 `get_provider_for_agent` | Modify |
| `src/everbot/core/runtime/heartbeat.py` | `_get_or_create_agent` 走 provider | Modify |
| `src/everbot/cli/daemon.py` | stop() 清理段调 shutdown_sidecars | Modify |
| `tests/unit/test_milkie_launcher.py` | launcher 单测 | Create |
| `tests/unit/test_milkie_pool.py` | pool 单测 | Create |
| `tests/unit/test_provider_routing.py` | get_provider_for_agent 单测 | Create |
| `tests/unit/test_milkie_resume.py` | resume 流式映射单测 | Create |
| `tests/e2e/test_milkie_daemon_smoke.py` | create_agent 经池 + shutdown 子进程退出 e2e | Create |

---

## Task 1: SidecarLauncher — 配置翻译成 serve 命令

把一个 agent 的 system_prompt + dolphin 模型档翻译成 `milkie serve` 的命令行、env、data-dir。纯函数,不 spawn。

**Files:**
- Create: `src/everbot/core/agent/provider/milkie/launcher.py`
- Test: `tests/unit/test_milkie_launcher.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_milkie_launcher.py
from pathlib import Path

import pytest

from everbot.core.agent.provider.milkie.launcher import SidecarLauncher, LaunchSpec


def _launcher(tmp_path):
    return SidecarLauncher(
        dist_path=tmp_path / "milkie" / "dist" / "cli" / "index.js",
        data_dir_root=tmp_path / "data",
        node_bin="node",
        llms={"main": {"cloud": "oa", "model_name": "gpt-x", "type_api": "openai"},
              "fast": {"cloud": "oa", "model_name": "gpt-fast", "type_api": "openai"}},
        clouds={"oa": {"api": "https://api.oa/v1", "api_key": "sk-real"}},
        default_model="main",
        fast_model="fast",
    )


def test_build_writes_agent_md_and_returns_cmd(tmp_path):
    spec = _launcher(tmp_path).build("alice", system_prompt="You are Alice.")
    assert isinstance(spec, LaunchSpec)
    # agent.md 落盘在 data_dir 下且内容含 system_prompt + 两档 model
    assert spec.agent_md.exists()
    text = spec.agent_md.read_text(encoding="utf-8")
    assert "You are Alice." in text
    assert "gpt-x" in text and "gpt-fast" in text
    # data-dir 预建
    assert spec.data_dir.is_dir()
    # 命令形态:node <dist> serve --agent <md> --port 0 --state-store sqlite --data-dir <dir>
    assert spec.cmd[0] == "node"
    assert spec.cmd[1].endswith("index.js")
    assert "serve" in spec.cmd
    assert spec.cmd[spec.cmd.index("--agent") + 1] == str(spec.agent_md)
    assert spec.cmd[spec.cmd.index("--port") + 1] == "0"
    assert spec.cmd[spec.cmd.index("--state-store") + 1] == "sqlite"
    assert spec.cmd[spec.cmd.index("--data-dir") + 1] == str(spec.data_dir)


def test_build_injects_cloud_api_key_env(tmp_path):
    spec = _launcher(tmp_path).build("alice", system_prompt="x")
    # OpenAICompatibleAdapter 仅从 env 读 key → 注入 cloud 的 api_key
    assert spec.env.get("OPENAI_API_KEY") == "sk-real"


def test_build_unknown_model_fails_fast(tmp_path):
    launcher = SidecarLauncher(
        dist_path=tmp_path / "x.js", data_dir_root=tmp_path / "d", node_bin="node",
        llms={}, clouds={}, default_model="missing", fast_model="missing",
    )
    with pytest.raises(KeyError):
        launcher.build("alice", system_prompt="x")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_milkie_launcher.py -q`
Expected: FAIL — `ModuleNotFoundError: ...launcher`。

- [ ] **Step 3: Write minimal implementation**

```python
# src/everbot/core/agent/provider/milkie/launcher.py
"""alfred agent 配置 → ``milkie serve`` 命令 + env + data-dir(纯函数)。

复用 :mod:`agent_spec` 生成 agent.md(两档 model);per-cloud api_key 注入 env
(milkie OpenAICompatibleAdapter 仅从 env 读 key)。data-dir 预建(SQLiteStore 不自建)。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from .agent_spec import build_milkie_agent_md, build_milkie_model_tiers


@dataclass
class LaunchSpec:
    cmd: List[str]
    env: Dict[str, str]
    data_dir: Path
    agent_md: Path


class SidecarLauncher:
    def __init__(
        self,
        *,
        dist_path: Path,
        data_dir_root: Path,
        node_bin: str,
        llms: Dict[str, Any],
        clouds: Dict[str, Any],
        default_model: str,
        fast_model: str,
    ) -> None:
        self._dist_path = Path(dist_path)
        self._data_dir_root = Path(data_dir_root)
        self._node_bin = node_bin
        self._llms = llms
        self._clouds = clouds
        self._default_model = default_model
        self._fast_model = fast_model

    def build(self, agent_name: str, *, system_prompt: str) -> LaunchSpec:
        tiers = build_milkie_model_tiers(
            self._llms, self._clouds, default=self._default_model, fast=self._fast_model
        )  # 未知 model → KeyError(fail fast)
        data_dir = (self._data_dir_root / agent_name).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        agent_md = data_dir / "agent.md"
        agent_md.write_text(
            build_milkie_agent_md(agent_name, system_prompt, tiers), encoding="utf-8"
        )
        cmd = [
            self._node_bin, str(self._dist_path.expanduser()), "serve",
            "--agent", str(agent_md), "--port", "0",
            "--state-store", "sqlite", "--data-dir", str(data_dir),
        ]
        env = dict(os.environ)
        # 默认档 cloud 的 key 注入(单 cloud 场景;跨 cloud per-model key 是已知 milkie gap)。
        default_cloud = self._llms[self._default_model]["cloud"]
        api_key = self._clouds[default_cloud].get("api_key")
        if api_key:
            env["OPENAI_API_KEY"] = api_key
        return LaunchSpec(cmd=cmd, env=env, data_dir=data_dir, agent_md=agent_md)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_milkie_launcher.py -q`
Expected: PASS(3 passed)。

- [ ] **Step 5: Commit**

```bash
git add src/everbot/core/agent/provider/milkie/launcher.py tests/unit/test_milkie_launcher.py
git commit -m "feat(milkie): SidecarLauncher — agent 配置→serve 命令+env+data-dir"
```

---

## Task 2: SidecarPool — 惰性 spawn + 并发安全 + 统一关闭

**Files:**
- Create: `src/everbot/core/agent/provider/milkie/pool.py`
- Test: `tests/unit/test_milkie_pool.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_milkie_pool.py
import asyncio

import pytest

from everbot.core.agent.provider.milkie.pool import SidecarPool


class _FakeSidecar:
    instances = []

    def __init__(self, cmd, env=None, ready_timeout=10.0):
        self.cmd = cmd
        self.started = 0
        self.closed = 0
        self.port = 18000 + len(_FakeSidecar.instances)
        _FakeSidecar.instances.append(self)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    async def start(self):
        self.started += 1
        await asyncio.sleep(0)  # 让出,放大并发竞争窗口

    async def close(self):
        self.closed += 1


@pytest.fixture(autouse=True)
def _reset():
    _FakeSidecar.instances = []
    yield


def _pool():
    return SidecarPool(
        build=lambda name: (["node", "serve", name], {"K": "v"}),
        sidecar_factory=lambda cmd, env: _FakeSidecar(cmd, env),
    )


async def test_lazy_spawn_then_reuse():
    pool = _pool()
    s1 = await pool.get_or_spawn("alice")
    s2 = await pool.get_or_spawn("alice")
    assert s1 is s2                      # 同 agent 复用
    assert s1.started == 1               # 只 spawn 一次
    assert len(_FakeSidecar.instances) == 1


async def test_concurrent_same_agent_spawns_once():
    pool = _pool()
    results = await asyncio.gather(*[pool.get_or_spawn("alice") for _ in range(10)])
    assert all(r is results[0] for r in results)   # 全是同一个
    assert results[0].started == 1                 # 锁保证只 spawn 一次
    assert len(_FakeSidecar.instances) == 1


async def test_distinct_agents_distinct_sidecars():
    pool = _pool()
    a = await pool.get_or_spawn("alice")
    b = await pool.get_or_spawn("bob")
    assert a is not b
    assert {a.cmd[-1], b.cmd[-1]} == {"alice", "bob"}


async def test_shutdown_all_closes_and_clears():
    pool = _pool()
    a = await pool.get_or_spawn("alice")
    b = await pool.get_or_spawn("bob")
    await pool.shutdown_all()
    assert a.closed == 1 and b.closed == 1
    await pool.shutdown_all()             # 幂等
    assert a.closed == 1                  # 不重复关闭
    # 关闭后再取 → 重新 spawn
    c = await pool.get_or_spawn("alice")
    assert c is not a


async def test_spawn_failure_not_cached():
    calls = {"n": 0}

    def factory(cmd, env):
        calls["n"] += 1
        s = _FakeSidecar(cmd, env)
        if calls["n"] == 1:
            async def boom():
                raise RuntimeError("ready timeout")
            s.start = boom
        return s

    pool = SidecarPool(build=lambda name: (["c"], {}), sidecar_factory=factory)
    with pytest.raises(RuntimeError):
        await pool.get_or_spawn("alice")
    # 失败不入池 → 下次重试能成功
    s = await pool.get_or_spawn("alice")
    assert s.started == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_milkie_pool.py -q`
Expected: FAIL — `ModuleNotFoundError: ...pool`。

- [ ] **Step 3: Write minimal implementation**

```python
# src/everbot/core/agent/provider/milkie/pool.py
"""按 agent_name 维度管理 milkie serve 子进程:惰性 spawn + 常驻复用 + 统一关闭。

并发同 agent 经 per-agent 锁串行化,只 spawn 一次。spawn 失败不入池(下次重试)。
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, Tuple

from .sidecar import MilkieSidecar


def _default_factory(cmd, env):
    return MilkieSidecar(cmd, env=env)


class SidecarPool:
    def __init__(
        self,
        *,
        build: Callable[[str], Tuple[list, dict]],
        sidecar_factory: Callable[[list, dict], Any] = _default_factory,
    ) -> None:
        self._build = build                  # agent_name -> (cmd, env)
        self._factory = sidecar_factory
        self._sidecars: Dict[str, Any] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def _lock(self, agent_name: str) -> asyncio.Lock:
        lock = self._locks.get(agent_name)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[agent_name] = lock
        return lock

    async def get_or_spawn(self, agent_name: str) -> Any:
        existing = self._sidecars.get(agent_name)
        if existing is not None:
            return existing
        async with self._lock(agent_name):
            existing = self._sidecars.get(agent_name)   # 双检:等锁期间可能已被建好
            if existing is not None:
                return existing
            cmd, env = self._build(agent_name)
            sidecar = self._factory(cmd, env)
            await sidecar.start()                        # 失败则抛、不入池
            self._sidecars[agent_name] = sidecar
            return sidecar

    async def shutdown_all(self) -> None:
        sidecars = list(self._sidecars.values())
        self._sidecars.clear()
        if sidecars:
            await asyncio.gather(
                *(s.close() for s in sidecars), return_exceptions=True
            )
```

注意:`MilkieSidecar.__init__` 当前签名是 `(cmd, *, ready_timeout=10.0)`,无 `env` 参数。下一步在 Task 2b 补 env 透传;此处 `_default_factory` 先按将补的签名写。

- [ ] **Step 3b: 给 MilkieSidecar 加 env 透传**

修改 `src/everbot/core/agent/provider/milkie/sidecar.py`:

```python
# __init__ 签名改为:
    def __init__(self, cmd: List[str], *, env: Optional[dict] = None, ready_timeout: float = 10.0) -> None:
        self._cmd = cmd
        self._env = env
        self._ready_timeout = ready_timeout
        self._proc: Optional[asyncio.subprocess.Process] = None
        self.port: Optional[int] = None

# start() 里 create_subprocess_exec 增加 env=self._env:
        self._proc = await asyncio.create_subprocess_exec(
            *self._cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            env=self._env,
        )
```

并在 `tests/unit/test_milkie_sidecar.py` 末尾追加一条守护测试:

```python
async def test_sidecar_passes_env(monkeypatch):
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        raise RuntimeError("stop here")  # 不真跑

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    sc = MilkieSidecar(["node", "x"], env={"OPENAI_API_KEY": "sk"})
    with pytest.raises(RuntimeError):
        await sc.start()
    assert captured["env"] == {"OPENAI_API_KEY": "sk"}
```

(若 `tests/unit/test_milkie_sidecar.py` 未 import pytest,补 `import pytest`。)

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_milkie_pool.py tests/unit/test_milkie_sidecar.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/everbot/core/agent/provider/milkie/pool.py src/everbot/core/agent/provider/milkie/sidecar.py tests/unit/test_milkie_pool.py tests/unit/test_milkie_sidecar.py
git commit -m "feat(milkie): SidecarPool 惰性spawn+并发安全+统一关闭;sidecar env 透传"
```

---

## Task 3: MilkieProvider 接 launcher+pool + shutdown_sidecars

让 `create_agent` 走池,返回带该 serve 各自 `base_url` 的 handle。Provider 自己从 alfred config + dolphin.yaml 装配 launcher。

**Files:**
- Modify: `src/everbot/core/agent/provider/milkie/provider.py`
- Test: `tests/unit/test_milkie_provider.py`(追加)

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_milkie_provider.py(追加)
import pytest

from everbot.core.agent.provider.milkie.provider import MilkieProvider, MilkieAgentHandle


class _FakeSidecarStub:
    def __init__(self):
        self.closed = 0
    @property
    def base_url(self):
        return "http://127.0.0.1:19999"
    async def close(self):
        self.closed += 1


async def test_create_agent_uses_pool_base_url(monkeypatch):
    stub = _FakeSidecarStub()

    class _FakePool:
        async def get_or_spawn(self, name):
            return stub
        async def shutdown_all(self):
            await stub.close()

    prov = MilkieProvider.__new__(MilkieProvider)   # 跳过 __init__ 的 config 读取
    prov._base_url = None
    prov._client = None
    prov._sync_client = None
    prov._pool = _FakePool()
    prov._system_prompt_loader = lambda name: "sys"

    handle = await prov.create_agent("alice", workspace_path="/tmp")
    assert isinstance(handle, MilkieAgentHandle)
    assert handle.base_url == "http://127.0.0.1:19999"   # 来自池内 serve,非固定 config
    assert handle.context_id.startswith("alice-")

    await prov.shutdown_sidecars()
    assert stub.closed == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_milkie_provider.py::test_create_agent_uses_pool_base_url -q`
Expected: FAIL — `AttributeError: 'MilkieProvider' object has no attribute '_pool'` 或 create_agent 仍用旧固定 base_url。

- [ ] **Step 3: Write minimal implementation**

修改 `src/everbot/core/agent/provider/milkie/provider.py`:

1) `__init__` 装配 pool(从 config 读 milkie 设置 + dolphin.yaml 读 llms/clouds),保留测试注入入口:

```python
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
        self._client = client
        self._sync_client = sync_client
        self._pool = pool if pool is not None else self._build_pool()
        self._system_prompt_loader = system_prompt_loader or _default_system_prompt_loader

    @staticmethod
    def _build_pool():
        from .launcher import SidecarLauncher
        from .pool import SidecarPool
        from ....infra.config import get_config

        cfg = (get_config() or {}).get("everbot", {}) or {}
        milkie_cfg = cfg.get("milkie", {}) or {}
        repo_root = Path(__file__).resolve().parents[6]   # …/alfred
        dist_path = Path(milkie_cfg.get("dist_path") or (repo_root.parent / "milkie" / "dist" / "cli" / "index.js"))
        data_dir_root = Path(milkie_cfg.get("data_dir_root") or "~/.alfred/milkie").expanduser()
        node_bin = milkie_cfg.get("node_bin") or "node"

        # dolphin.yaml(llms/clouds)+ 默认/快档 model 名
        from ..dolphin.factory import get_agent_factory
        factory = get_agent_factory()
        dolphin_path = getattr(factory, "global_config_path", None)
        import yaml as _yaml
        dolphin_cfg = {}
        if dolphin_path and Path(dolphin_path).exists():
            dolphin_cfg = _yaml.safe_load(Path(dolphin_path).read_text(encoding="utf-8")) or {}
        llms = dolphin_cfg.get("llms", {}) or {}
        clouds = dolphin_cfg.get("clouds", {}) or {}
        default_model = dolphin_cfg.get("default_model") or next(iter(llms), "")
        fast_model = dolphin_cfg.get("fast_llm") or default_model

        launcher = SidecarLauncher(
            dist_path=dist_path, data_dir_root=data_dir_root, node_bin=node_bin,
            llms=llms, clouds=clouds, default_model=default_model, fast_model=fast_model,
        )
        ready_timeout = float(milkie_cfg.get("ready_timeout", 20.0))

        def _build(agent_name: str):
            spec = launcher.build(agent_name, system_prompt=_default_system_prompt_loader(agent_name))
            return spec.cmd, spec.env

        from .sidecar import MilkieSidecar
        return SidecarPool(
            build=_build,
            sidecar_factory=lambda cmd, env: MilkieSidecar(cmd, env=env, ready_timeout=ready_timeout),
        )
```

2) `create_agent` 走池:

```python
    async def create_agent(self, agent_name, workspace_path, *, model_name=None,
                           extra_variables=None, tools_override=None) -> MilkieAgentHandle:
        sidecar = await self._pool.get_or_spawn(agent_name)
        return MilkieAgentHandle(
            base_url=sidecar.base_url,
            context_id=f"{agent_name}-{uuid.uuid4().hex[:8]}",
        )
```

3) 新增 `shutdown_sidecars` + 顶部补 `from pathlib import Path` 和 system_prompt loader:

```python
    async def shutdown_sidecars(self) -> None:
        await self._pool.shutdown_all()
```

```python
def _default_system_prompt_loader(agent_name: str) -> str:
    """从 agent workspace 读 system prompt。占位实现:读 ~/.alfred/agents/<name>/agent.md
    的 body;实现期对齐 dolphin factory 的真实 prompt 来源。"""
    from pathlib import Path as _P
    p = _P(f"~/.alfred/agents/{agent_name}/agent.md").expanduser()
    return p.read_text(encoding="utf-8") if p.exists() else ""
```

> **实现期校准**:`_default_system_prompt_loader` 的真实来源需对齐 dolphin `AgentFactory.create_agent`
> 加载 agent 定义的逻辑(读 `factory.py` 里 agent 配置/prompt 的加载点)。先用占位通过单测,
> 在 Task 8 集成前替换为真实 loader 并补 e2e 覆盖。

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_milkie_provider.py -q`
Expected: PASS(含原有用例)。

- [ ] **Step 5: Commit**

```bash
git add src/everbot/core/agent/provider/milkie/provider.py tests/unit/test_milkie_provider.py
git commit -m "feat(milkie): MilkieProvider 接 launcher+pool,create_agent 走池+shutdown_sidecars"
```

---

## Task 4: MilkieProvider.resume 实现(/resume 流式当新一轮)

**Files:**
- Modify: `src/everbot/core/agent/provider/milkie/provider.py`
- Test: `tests/unit/test_milkie_resume.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_milkie_resume.py
import json

import httpx
import pytest

from everbot.core.agent.provider.milkie.provider import MilkieProvider, MilkieAgentHandle


async def test_resume_posts_to_resume_and_consumes_stream():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = json.loads(request.content.decode())
        # serve /resume 是 SSE 流;这里回两帧 message_delta + 终态
        body = (
            'event: message_delta\ndata: {"delta":"hi"}\n\n'
            'event: agent.run.completed\ndata: {"status":"completed","output":"hi"}\n\n'
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    prov = MilkieProvider.__new__(MilkieProvider)
    prov._base_url = "http://x"
    prov._client = client
    prov._sync_client = None
    prov._pool = None

    handle = MilkieAgentHandle(base_url="http://x", context_id="alice-1")
    await prov.resume(handle, "continue please")   # 不再抛 NotImplementedError

    assert seen["url"].endswith("/resume")
    assert seen["json"]["contextId"] == "alice-1"
    assert seen["json"]["input"] == "continue please"
    await client.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_milkie_resume.py -q`
Expected: FAIL — `NotImplementedError`。

- [ ] **Step 3: Write minimal implementation**

替换 `provider.py` 的 `resume`:

```python
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
                async for _ in resp.aiter_text():
                    pass   # 排空流,确保 serve 续跑完成
        finally:
            if owns:
                await client.aclose()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_milkie_resume.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/everbot/core/agent/provider/milkie/provider.py tests/unit/test_milkie_resume.py
git commit -m "feat(milkie): 实现 resume — /resume 流式续跑(去 NotImplementedError)"
```

---

## Task 5: get_provider_for_agent — per-agent 路由 + telegram 自动回退

**Files:**
- Modify: `src/everbot/core/agent/provider/__init__.py`
- Modify: `src/everbot/core/agent/provider/dolphin/provider.py`(加 `shutdown_sidecars` no-op)
- Test: `tests/unit/test_provider_routing.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_provider_routing.py
import pytest

from everbot.core.agent.provider import get_provider_for_agent, reset_provider


@pytest.fixture(autouse=True)
def _reset():
    reset_provider()
    yield
    reset_provider()


def _cfg(monkeypatch, everbot):
    import everbot.core.agent.provider as mod
    monkeypatch.setattr(mod, "_load_everbot_cfg", lambda: everbot)


def test_explicit_agent_provider_wins(monkeypatch):
    _cfg(monkeypatch, {"provider": "dolphin",
                       "agents": {"alice": {"provider": "milkie"}}})
    assert type(get_provider_for_agent("alice")).__name__ == "MilkieProvider"


def test_global_milkie_telegram_agent_falls_back_to_dolphin(monkeypatch):
    _cfg(monkeypatch, {
        "provider": "milkie",
        "channels": {"telegram": {"enabled": True, "default_agent": "alice"}},
        "agents": {},
    })
    # alice 经 telegram 服务且未显式声明 → 回退 dolphin
    assert type(get_provider_for_agent("alice")).__name__ == "DolphinProvider"


def test_global_milkie_non_telegram_agent_uses_milkie(monkeypatch):
    _cfg(monkeypatch, {
        "provider": "milkie",
        "channels": {"telegram": {"enabled": True, "default_agent": "alice"}},
        "agents": {},
    })
    assert type(get_provider_for_agent("bob")).__name__ == "MilkieProvider"


def test_global_milkie_multibot_telegram_detection(monkeypatch):
    _cfg(monkeypatch, {
        "provider": "milkie",
        "channels": {"telegram": [
            {"enabled": True, "default_agent": "alice"},
            {"enabled": True, "default_agent": "dev"},
        ]},
        "agents": {},
    })
    assert type(get_provider_for_agent("dev")).__name__ == "DolphinProvider"
    assert type(get_provider_for_agent("other")).__name__ == "MilkieProvider"


def test_explicit_milkie_telegram_agent_respected(monkeypatch):
    # 显式配 milkie → 尊重意图,不强制回退
    _cfg(monkeypatch, {
        "provider": "milkie",
        "channels": {"telegram": {"enabled": True, "default_agent": "alice"}},
        "agents": {"alice": {"provider": "milkie"}},
    })
    assert type(get_provider_for_agent("alice")).__name__ == "MilkieProvider"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_provider_routing.py -q`
Expected: FAIL — `ImportError: cannot import name 'get_provider_for_agent'`。

- [ ] **Step 3: Write minimal implementation**

在 `src/everbot/core/agent/provider/__init__.py` 增加(保留现有 `get_provider`/`reset_provider`):

```python
import logging

logger = logging.getLogger(__name__)

_warned_fallback: set = set()


def _load_everbot_cfg() -> dict:
    from ....infra.config import get_config
    return (get_config() or {}).get("everbot", {}) or {}


def _telegram_serving_agents(everbot_cfg: dict) -> set:
    """收集 telegram 频道绑定的 default_agent(单 bot dict 或多 bot list)。"""
    tg = (everbot_cfg.get("channels", {}) or {}).get("telegram")
    agents: set = set()
    if isinstance(tg, dict):
        if tg.get("enabled") and tg.get("default_agent"):
            agents.add(tg["default_agent"])
    elif isinstance(tg, list):
        for c in tg:
            if isinstance(c, dict) and c.get("enabled", True) and c.get("default_agent"):
                agents.add(c["default_agent"])
    return agents


def _make_provider(name: str) -> "AgentProvider":
    if name == "milkie":
        from .milkie.provider import MilkieProvider
        return MilkieProvider()
    from .dolphin.provider import DolphinProvider
    return DolphinProvider()


_per_agent_singletons: dict = {}


def get_provider_for_agent(agent_name: str) -> "AgentProvider":
    """Per-agent provider 路由:显式配置 > telegram 自动回退 > 全局。"""
    everbot_cfg = _load_everbot_cfg()
    agent_cfg = (everbot_cfg.get("agents", {}) or {}).get(agent_name, {}) or {}
    explicit = agent_cfg.get("provider")
    global_name = everbot_cfg.get("provider") or "dolphin"

    if explicit:
        chosen = explicit
    elif global_name == "milkie" and agent_name in _telegram_serving_agents(everbot_cfg):
        chosen = "dolphin"
        if agent_name not in _warned_fallback:
            _warned_fallback.add(agent_name)
            logger.warning(
                "Agent '%s' 经 telegram 服务但 milkie 暂不支持 telegram skillkit"
                "(待 milkie#87),自动回退 dolphin。如确需 milkie 请显式配置 "
                "everbot.agents.%s.provider=milkie。", agent_name, agent_name,
            )
    else:
        chosen = global_name

    cached = _per_agent_singletons.get(chosen)
    if cached is None:
        cached = _make_provider(chosen)
        _per_agent_singletons[chosen] = cached
    return cached
```

并在 `reset_provider()` 末尾清掉 per-agent 缓存与 warning 记忆:

```python
def reset_provider() -> None:
    global _provider_singleton
    _provider_singleton = None
    _per_agent_singletons.clear()
    _warned_fallback.clear()
```

把 `get_provider_for_agent` 加进 `__all__`。

DolphinProvider 加 no-op(`src/everbot/core/agent/provider/dolphin/provider.py`,类内任意位置):

```python
    async def shutdown_sidecars(self) -> None:
        return None  # dolphin 进程内,无 sidecar
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_provider_routing.py -q`
Expected: PASS(5 passed)。

- [ ] **Step 5: Commit**

```bash
git add src/everbot/core/agent/provider/__init__.py src/everbot/core/agent/provider/dolphin/provider.py tests/unit/test_provider_routing.py
git commit -m "feat(provider): get_provider_for_agent — per-agent 路由+telegram 自动回退 dolphin"
```

---

## Task 6: 创建路径收敛 — agent_service / heartbeat 走 provider

**Files:**
- Modify: `src/everbot/core/agent/agent_service.py:43-58`
- Modify: `src/everbot/core/runtime/heartbeat.py:1364`(`_get_or_create_agent`)
- Test: `tests/unit/test_create_path_routing.py`(Create)

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_create_path_routing.py
import pytest

from everbot.core.agent.agent_service import AgentService


async def test_create_agent_instance_routes_through_provider(monkeypatch, tmp_path):
    called = {}

    class _FakeProvider:
        async def create_agent(self, name, workspace_path, **kw):
            called["name"] = name
            called["ws"] = workspace_path
            return f"agent::{name}"

    import everbot.core.agent.agent_service as svc
    monkeypatch.setattr(svc, "get_provider_for_agent", lambda name: _FakeProvider())

    service = AgentService()
    # 让 agent_dir 存在
    agent_dir = tmp_path / "alice"
    agent_dir.mkdir()
    monkeypatch.setattr(service.user_data, "get_agent_dir", lambda n: agent_dir)
    monkeypatch.setattr(svc, "ensure_continue_chat_compatibility", lambda: None)

    agent = await service.create_agent_instance("alice")
    assert agent == "agent::alice"
    assert called["name"] == "alice"
    assert called["ws"] == agent_dir
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_create_path_routing.py -q`
Expected: FAIL — `create_agent_instance` 仍调旧 `create_agent`(dolphin factory),`get_provider_for_agent` 未被引用。

- [ ] **Step 3: Write minimal implementation**

`agent_service.py`:把 `from .factory import create_agent` 换/补为 provider 路由,改 `create_agent_instance`:

```python
# 顶部 import 增加:
from .provider import get_provider_for_agent

# create_agent_instance 内最后一行替换:
        agent = await get_provider_for_agent(agent_name).create_agent(agent_name, agent_dir)
        return agent
```

`heartbeat.py` 的 `_get_or_create_agent`(行 1364 附近):把内部直接用 `agent_factory.create_agent` / `create_agent` 的调用改为:

```python
        from ..agent.provider import get_provider_for_agent
        agent = await get_provider_for_agent(self.agent_name).create_agent(
            self.agent_name, self._workspace_path,
        )
```

(保留 `_get_or_create_agent` 既有的缓存/复用逻辑,只替换实际创建那一步;`self._workspace_path` 用该 runner 现有的 workspace 字段名,实现期对齐。)

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_create_path_routing.py tests/unit/ -q -k "agent_service or heartbeat or create_path"`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/everbot/core/agent/agent_service.py src/everbot/core/runtime/heartbeat.py tests/unit/test_create_path_routing.py
git commit -m "feat(provider): 创建路径收敛 — agent_service/heartbeat 经 get_provider_for_agent"
```

---

## Task 7: daemon 生命周期 — stop() 关闭 sidecar 池

**Files:**
- Modify: `src/everbot/cli/daemon.py`(`stop()` 清理段)
- Test: `tests/unit/test_daemon_shutdown_sidecars.py`(Create)

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_daemon_shutdown_sidecars.py
import pytest

from everbot.cli.daemon import EverBotDaemon


async def test_stop_calls_provider_shutdown_sidecars(monkeypatch):
    closed = {"n": 0}

    class _FakeProvider:
        async def shutdown_sidecars(self):
            closed["n"] += 1

    import everbot.cli.daemon as dmod
    monkeypatch.setattr(dmod, "get_provider", lambda: _FakeProvider())

    daemon = EverBotDaemon.__new__(EverBotDaemon)
    daemon._shutdown_requested = True
    daemon._running = True
    daemon._telegram_channels = []
    daemon._scheduler = None
    daemon.heartbeat_runners = {}
    monkeypatch.setattr(daemon, "request_shutdown", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_write_status_snapshot", lambda: None)

    await daemon.stop()
    assert closed["n"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_daemon_shutdown_sidecars.py -q`
Expected: FAIL — stop() 未调 shutdown_sidecars。

- [ ] **Step 3: Write minimal implementation**

`daemon.py` 顶部确保 `from ..core.agent.provider import get_provider`(已有则复用);在 `stop()` 的 `for runner in self.heartbeat_runners.values(): runner.stop()` 之后、`self._write_status_snapshot()` 之前插入:

```python
        try:
            await get_provider().shutdown_sidecars()
        except Exception as exc:
            logger.warning("shutdown_sidecars error: %s", exc)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_daemon_shutdown_sidecars.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/everbot/cli/daemon.py tests/unit/test_daemon_shutdown_sidecars.py
git commit -m "feat(daemon): stop() 统一关闭 milkie sidecar 池(dolphin no-op)"
```

---

## Task 8: 真实 system_prompt loader + 收敛主路径裸 context 访问

把 Task 3 占位的 `_default_system_prompt_loader` 换成对齐 dolphin 的真实来源;收敛 daemon 主路径会触达的 `agent.executor.context` 裸访问。

**Files:**
- Modify: `src/everbot/core/agent/provider/milkie/provider.py`(loader)
- Modify: 按 Explore 定位逐处(`persistence.py` / `session.py` / `heartbeat.py` 主路径点)
- Test: `tests/unit/test_milkie_system_prompt_loader.py`(Create)

- [ ] **Step 1: 先读 dolphin factory 确定 prompt 真实来源**

Run: `grep -n "system_prompt\|systemPrompt\|prompt\|agent.md\|\.dph\|read_text\|instructions" src/everbot/core/agent/provider/dolphin/factory.py | head -30`
据此确定 agent 定义文件与 system_prompt 字段,写进 loader。

- [ ] **Step 2: Write the failing test**

```python
# tests/unit/test_milkie_system_prompt_loader.py
from everbot.core.agent.provider.milkie.provider import _default_system_prompt_loader


def test_loader_reads_agent_definition(tmp_path, monkeypatch):
    # 按 Step 1 查到的真实路径/格式构造 fixture,断言 loader 取到 system prompt
    # (占位:实现期据 factory 的真实来源替换断言)
    agent_home = tmp_path / "alice"
    agent_home.mkdir()
    (agent_home / "agent.md").write_text("---\nx: 1\n---\nYou are Alice.", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path.parent))   # 或 monkeypatch loader 的根路径
    # 视真实来源调整;此处示意取到 body
    text = _default_system_prompt_loader.__wrapped__("alice") if hasattr(
        _default_system_prompt_loader, "__wrapped__") else None
    assert text is None or "Alice" in (text or "")
```

> **实现期**:此测试按 Step 1 的真实来源改写为确定断言(去掉 None 兜底)。loader 不得静默返回空 —— agent 定义缺失应 raise,避免跑出无 prompt 的 milkie agent。

- [ ] **Step 3: 替换 loader 实现**

据 Step 1 结论实现真实 loader(读 agent 定义、抽 system_prompt);agent 定义缺失 → `FileNotFoundError`。

- [ ] **Step 4: 收敛主路径裸 context 访问**

对 Explore 定位的主路径点(daemon turn/heartbeat 会触达者),改为经 provider 接口或加能力守护:

```python
        # 形如:原 `ctx = agent.executor.context` 的点
        # 若该点逻辑是读写 var → 改 get_provider().get_variable/set_variable(agent, ...)
        # 若是 dolphin-only 持久化(milkie 已 short-circuit)→ 加守护:
        from ..agent.provider import get_provider
        if get_provider().needs_history_restore():   # 仅 dolphin 进入
            ctx = agent.executor.context
            ...
```

逐处加/改后跑该文件相关测试确认绿。

- [ ] **Step 5: Run + Commit**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_milkie_system_prompt_loader.py tests/unit/test_milkie_provider.py -q`
Expected: PASS。

```bash
git add -A
git commit -m "feat(milkie): 真实 system_prompt loader + 收敛主路径裸 context 访问"
```

---

## Task 9: #4 软翻默认 + e2e + 全量回归

**Files:**
- Modify: 示例/默认配置(`config.example.yaml` 或 README;`get_provider` 默认仍读 config,不硬改默认值——通过文档+示例把默认设 milkie)
- Create: `tests/e2e/test_milkie_daemon_smoke.py`
- Modify: `goal.md`(回写进度)

- [ ] **Step 1: e2e — create_agent 经池 + daemon shutdown 子进程退出**

```python
# tests/e2e/test_milkie_daemon_smoke.py
"""E2E:MilkieProvider 经 SidecarPool 真 spawn serve → create_agent 拿到 handle →
run_turn 跑通逐 token → shutdown_sidecars 后子进程已退出。需 ../milkie 已 build。"""
import sys
from pathlib import Path

import pytest

from everbot.core.agent.provider.milkie.pool import SidecarPool
from everbot.core.agent.provider.milkie.provider import MilkieProvider
from everbot.core.agent.provider.milkie.agent_spec import build_milkie_model_tiers, build_milkie_agent_md

CLI = Path(__file__).resolve().parents[2].parent / "milkie" / "dist" / "cli" / "index.js"


@pytest.mark.skipif(not CLI.exists(), reason="milkie dist not built")
async def test_pool_spawn_create_agent_and_shutdown(tmp_path, fake_openai_port):
    # 复用 test_milkie_serve_smoke 的 fake OpenAI fixture(同 conftest)
    base = f"http://127.0.0.1:{fake_openai_port}"
    llms = {"fake": {"cloud": "fc", "model_name": "fake-model", "type_api": "openai"}}
    clouds = {"fc": {"api": base, "api_key": "sk-fake"}}
    tiers = build_milkie_model_tiers(llms, clouds, default="fake", fast="fake")
    data_dir = tmp_path / "alice"
    data_dir.mkdir()
    agent_md = data_dir / "agent.md"
    agent_md.write_text(build_milkie_agent_md("alice", "You are Alice.", tiers), encoding="utf-8")

    def _build(name):
        return (["node", str(CLI), "serve", "--agent", str(agent_md), "--port", "0",
                 "--state-store", "sqlite", "--data-dir", str(data_dir)],
                {"OPENAI_API_KEY": "sk-fake", "PATH": __import__("os").environ.get("PATH", "")})

    pool = SidecarPool(build=_build)
    prov = MilkieProvider.__new__(MilkieProvider)
    prov._base_url = None; prov._client = None; prov._sync_client = None; prov._pool = pool

    handle = await prov.create_agent("alice", workspace_path=str(data_dir))
    assert handle.base_url.startswith("http://127.0.0.1:")
    sidecar = pool._sidecars["alice"]

    deltas = []
    async for ev in prov.run_turn(handle, "hi"):
        for item in ev.get("_progress", []):
            if item.get("type") == "LLM_DELTA":
                deltas.append(item)
    assert len(deltas) >= 1

    await prov.shutdown_sidecars()
    assert sidecar.returncode is not None   # 子进程已退出
```

> fake_openai_port fixture:若不在 conftest,从 `tests/e2e/test_milkie_serve_smoke.py` 抽到 `tests/e2e/conftest.py` 共享。

- [ ] **Step 2: Run e2e(本地已 build milkie)**

Run: `cd ../milkie && npm run build && cd -; PYTHONPATH=src .venv/bin/python -m pytest tests/e2e/test_milkie_daemon_smoke.py -q`
Expected: PASS(或未 build 时 skip)。

- [ ] **Step 3: 软翻默认 + 文档**

把示例配置/README 的默认 `everbot.provider` 设为 `milkie`,并写明:telegram-serving agent 自动回退 dolphin;如需纯 milkie 显式配 `everbot.agents.<name>.provider: milkie`;切回全 dolphin 设 `everbot.provider: dolphin`。`get_provider`/`get_provider_for_agent` 默认值代码层保持 `dolphin`(零配置安全),默认翻转通过发布配置体现。

- [ ] **Step 4: 全量回归(硬标准:双 provider 并存)**

Run:
```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_agent_provider_*.py -q   # 边界守护
```
Expected: 全绿(对齐 goal.md 的 1585 基线 + 新增用例)。

- [ ] **Step 5: 回写 goal.md + Commit**

更新 `goal.md` 第 4 阶段表与第 6 节:#3 daemon 集成完成项、#4 软翻默认、telegram 回退、剩余(milkie#87 telegram 原生发送以撤回退)。

```bash
git add -A
git commit -m "feat(milkie): #4 软翻默认+telegram 自动回退;daemon 集成 e2e;goal.md 回写"
```

---

## Self-Review(已执行)

- **Spec 覆盖**:launcher(§3.1→T1)、pool(§3.2→T2)、provider 升级(§3.3→T3/T4)、per-agent 路由(§3.4→T5)、创建路径收敛(§3.5→T6)、daemon 生命周期(§3.6→T7)、裸 context 收敛(§3.7→T8)、#4 软翻默认(§1/§2→T9)、配置键(§4→T3/T9)、错误处理(§5→T1 fail-fast/T2 spawn 失败/T5 回退)、测试(§6→各 Task + T9 e2e)。全覆盖。
- **占位**:T8/T3 的 system_prompt loader 标注「实现期据 dolphin factory 真实来源校准」——这是真实的实现期依赖(需先读 factory),非可填的死占位;已给出占位实现保证单测可跑,并明确替换条件(不得静默返回空)。
- **类型一致**:`LaunchSpec(cmd,env,data_dir,agent_md)`、`SidecarPool(build,sidecar_factory)`/`get_or_spawn`/`shutdown_all`、`MilkieProvider._pool`/`shutdown_sidecars`、`get_provider_for_agent`、`MilkieSidecar(cmd,env=,ready_timeout=)` 跨 Task 一致。
