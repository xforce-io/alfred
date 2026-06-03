import pytest

from everbot.cli.daemon import EverBotDaemon


async def test_stop_calls_provider_shutdown_sidecars(monkeypatch):
    closed = {"n": 0}

    async def _fake_shutdown_all():
        closed["n"] += 1

    import everbot.cli.daemon as dmod
    monkeypatch.setattr(dmod, "shutdown_all_providers", _fake_shutdown_all)

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
