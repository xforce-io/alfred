"""Tests for the web browser server lifecycle owner."""

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


SERVER = Path("skills/web/server.sh").resolve()


def test_health_check_bypasses_host_proxy():
    assert "curl --noproxy '*'" in SERVER.read_text()


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "WEB_SERVER_TMP_DIR": str(tmp_path),
        "WEB_SERVER_PID_FILE": str(tmp_path / "server.pid"),
        "WEB_SERVER_LOCK_DIR": str(tmp_path / "server.lock"),
        "WEB_SERVER_LOG_FILE": str(tmp_path / "server.log"),
        "WEB_SERVER_COMMAND": "exec sleep 30",
        "WEB_SERVER_ALLOW_TEST_COMMAND": "1",
        "WEB_SERVER_SKIP_HEALTH": "1",
        "WEB_SERVER_START_TIMEOUT_SECONDS": "2",
        "WEB_SERVER_CDP_PORT": "49223",
    }


def _run(action: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SERVER), action],
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_start_status_stop_uses_one_owned_pid(tmp_path):
    started = _run("start", tmp_path)
    assert started.returncode == 0, started.stderr
    pid = (tmp_path / "server.pid").read_text().strip()

    status = _run("status", tmp_path)
    assert status.returncode == 0
    assert pid in status.stdout

    stopped = _run("stop", tmp_path)
    assert stopped.returncode == 0, stopped.stderr
    assert not (tmp_path / "server.pid").exists()


def test_concurrent_starts_keep_a_single_owner(tmp_path):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _run("start", tmp_path), range(2)))

    assert [result.returncode for result in results] == [0, 0]
    pid = int((tmp_path / "server.pid").read_text().strip())
    assert pid > 0
    assert _run("stop", tmp_path).returncode == 0
