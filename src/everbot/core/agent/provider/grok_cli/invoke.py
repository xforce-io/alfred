"""Spawn grok CLI headless and map JSON stdout onto _progress items.

I/O (subprocess) is isolated behind an injectable ``runner`` so unit tests
never call the real binary. Mapping (JSON ``text`` → llm _progress) is
pure and separately asserted.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

GROK_BIN = "grok"
PROMPT_REL = Path("tmp") / "grok-cli" / "_prompt.md"
STDOUT_REL = Path("tmp") / "grok-cli" / "_grok_stdout.json"
DEFAULT_TIMEOUT_S = 600.0

Runner = Callable[..., None]


def resolve_grok_executable(cmd: str = GROK_BIN) -> str:
    """Resolve grok on PATH or ``~/.local/bin/grok``."""
    if Path(cmd).is_file():
        return cmd
    found = shutil.which(cmd)
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / cmd
    if fallback.is_file():
        return str(fallback)
    return cmd


def grok_cli_available() -> bool:
    """True when ``grok --version`` exits 0 (kairo ``_cli_available``)."""
    exe = resolve_grok_executable()
    try:
        r = subprocess.run(
            [exe, "--version"], capture_output=True, timeout=10, check=False
        )
        return r.returncode == 0
    except Exception:
        return False


def grok_json_to_progress(data: dict[str, Any]) -> list[dict[str, Any]]:
    """JSON stdout → dolphin-style llm _progress items.

    Success field is ``text`` (kairo #61). Error envelope is
    ``{"type":"error","message":...}`` and is raised before mapping.
    """
    if data.get("type") == "error":
        raise RuntimeError(f"grok 报错:{data.get('message')!r}")
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("grok stdout 缺 text 字段")
    return [{"stage": "llm", "delta": text, "answer": "", "id": "llm"}]


def grok_oneshot_text(
    prompt: str,
    *,
    cwd: Path,
    model: str = "",
    runner: Optional[Runner] = None,
) -> str:
    """Headless grok CLI → assistant text (oneshot / skill complete)."""
    data = invoke_grok(prompt, cwd=cwd, model=model, runner=runner)
    items = grok_json_to_progress(data)
    if not items:
        return ""
    return (items[0].get("answer") or items[0].get("delta") or "").strip()


def _child_env(base: Optional[dict[str, str]] = None) -> dict[str, str]:
    env = dict(base if base is not None else os.environ)
    env.pop("XAI_API_KEY", None)
    return env


def _has_terminal_json(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and (
        isinstance(data.get("text"), str) or data.get("type") == "error"
    )


def _stop_process_group(proc: subprocess.Popen) -> None:
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        pgid = None
    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=1)
    except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
        pass
    finally:
        try:
            if pgid is not None:
                os.killpg(pgid, signal.SIGKILL)
            elif proc.poll() is None:
                proc.kill()
        except (ProcessLookupError, OSError):
            pass
        if proc.poll() is None:
            proc.wait(timeout=5)


def default_grok_runner(
    cmd: str,
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdout_file: Path,
    timeout: Optional[float] = None,
) -> None:
    """Run grok and stop once its terminal JSON is complete."""
    exe = resolve_grok_executable(cmd)
    stdout_file.parent.mkdir(parents=True, exist_ok=True)
    limit = timeout or DEFAULT_TIMEOUT_S
    deadline = time.monotonic() + limit
    terminal = False
    with (
        open(stdout_file, "w", encoding="utf-8") as out,
        tempfile.TemporaryFile(mode="w+", encoding="utf-8") as err_file,
    ):
        proc = subprocess.Popen(
            [exe, *args],
            cwd=str(cwd),
            env=env,
            stdout=out,
            stderr=err_file,
            text=True,
            start_new_session=True,
        )
        try:
            while proc.poll() is None:
                if _has_terminal_json(stdout_file):
                    terminal = True
                    _stop_process_group(proc)
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"grok CLI timeout after {limit}s")
                time.sleep(0.05)
        finally:
            if proc.poll() is None:
                _stop_process_group(proc)
        terminal = terminal or _has_terminal_json(stdout_file)
        err_file.seek(0)
        err = err_file.read()
    if proc.returncode not in (0, None) and not terminal:
        msg = (err or "").strip()[:500] or f"exit {proc.returncode}"
        if not stdout_file.exists() or not stdout_file.read_text(encoding="utf-8").strip():
            raise RuntimeError(f"grok exited {proc.returncode}: {msg}")


def invoke_grok(
    prompt: str,
    *,
    cwd: Path,
    model: str = "",
    runner: Optional[Runner] = None,
    timeout: Optional[float] = None,
    environ: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Write prompt file, spawn grok headless, return parsed JSON object."""
    work = Path(cwd).resolve()
    scratch_root = work / PROMPT_REL.parent
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="run-", dir=scratch_root) as scratch_name:
        scratch = Path(scratch_name)
        prompt_path = scratch / PROMPT_REL.name
        stdout_path = scratch / STDOUT_REL.name
        prompt_path.write_text(prompt, encoding="utf-8")
        args = [
            "--prompt-file",
            str(prompt_path.relative_to(work)),
            "--output-format",
            "json",
            "--always-approve",
            "--no-plan",
        ]
        if model.strip().startswith("grok-"):
            args += ["-m", model.strip()]
        (runner or default_grok_runner)(
            GROK_BIN,
            args,
            cwd=work,
            env=_child_env(environ),
            stdout_file=stdout_path,
            timeout=timeout,
        )
        if not stdout_path.exists():
            raise RuntimeError(f"grok 无 stdout 输出:{stdout_path}")
        raw = stdout_path.read_text(encoding="utf-8").strip()
        if not raw:
            raise RuntimeError(f"grok stdout 为空:{stdout_path}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"grok stdout 非 JSON:{raw[:300]!r}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"grok stdout 非 object:{type(data).__name__}")
        return data
