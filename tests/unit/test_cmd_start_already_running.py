"""CLI start must exit cleanly when another daemon already holds the lock (#177)."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.everbot.infra.process import DaemonAlreadyRunningError


def test_cmd_start_already_running_exits_zero(capsys):
    """Lock conflict is a successful no-op for KeepAlive, not a crash loop."""
    # importlib avoids `src.everbot.cli.main` resolving to the exported main() function
    # from src.everbot.cli.__init__ in some import orders.
    cli_main = importlib.import_module("src.everbot.cli.main")

    async def _boom(_args):
        raise DaemonAlreadyRunningError(
            "Another EverBot daemon is already running (lock: /tmp/x)"
        )

    with patch.object(cli_main, "cmd_start_async", side_effect=_boom):
        with patch.object(cli_main, "configure_daemon_logging"):
            with patch.object(cli_main, "get_user_data_manager") as mock_udm:
                mock_udm.return_value.logs_dir = Path("/tmp")
                with patch.object(cli_main, "rotate_file_if_large"):
                    with pytest.raises(SystemExit) as exc_info:
                        cli_main.cmd_start(
                            SimpleNamespace(log_level="INFO", config=None)
                        )

    assert exc_info.value.code == 0
    err = capsys.readouterr().err
    assert "already running" in err.lower()
    assert "Traceback" not in err
