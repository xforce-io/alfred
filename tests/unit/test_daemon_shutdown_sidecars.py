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
