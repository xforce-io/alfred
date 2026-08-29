"""End-to-end process isolation for concurrent grok-cli turns."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from src.everbot.core.agent.provider.grok_cli.invoke import invoke_grok


def test_concurrent_grok_processes_return_own_terminal_output(tmp_path, monkeypatch):
    fake_grok = tmp_path / "fake-grok"
    fake_grok.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys, time\n"
        "args = sys.argv[1:]\n"
        "prompt = pathlib.Path(args[args.index('--prompt-file') + 1]).read_text()\n"
        "pathlib.Path(f'{prompt}.pid').write_text(str(os.getpid()))\n"
        "print(json.dumps({'text': prompt}), flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    fake_grok.chmod(0o755)
    monkeypatch.setattr(
        "src.everbot.core.agent.provider.grok_cli.invoke.resolve_grok_executable",
        lambda _cmd: str(fake_grok),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(invoke_grok, prompt, cwd=tmp_path, timeout=5)
            for prompt in ("heartbeat", "hi")
        ]
        results = [future.result(timeout=2)["text"] for future in futures]

    assert results == ["heartbeat", "hi"]
    for prompt in ("heartbeat", "hi"):
        pid = int((tmp_path / f"{prompt}.pid").read_text())
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError(f"grok process {pid} was not cleaned up")
    assert list((tmp_path / "tmp" / "grok-cli").iterdir()) == []
