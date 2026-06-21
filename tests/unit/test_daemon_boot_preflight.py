"""#91 PR4:daemon boot 接入 milkie preflight。

验证 daemon._run_milkie_boot_preflight 把 self.config 传给 enforce_boot_preflight,
且 native deps 致命时异常向上传播(start() 据此拒绝启动 → fail-fast)。
"""
import pytest

from src.everbot.cli import daemon as daemon_module


def _make_daemon(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "everbot:\n  enabled: true\n  milkie:\n    node_bin: /opt/homebrew/bin/node\n",
        encoding="utf-8",
    )
    return daemon_module.EverBotDaemon(config_path=str(cfg))


def test_boot_preflight_invoked_with_daemon_config(tmp_path, monkeypatch):
    d = _make_daemon(tmp_path)
    captured = {}
    monkeypatch.setattr(
        "src.everbot.cli.milkie_preflight.enforce_boot_preflight",
        lambda config, project_root, log: captured.update(config=config, log=log),
    )
    d._run_milkie_boot_preflight()
    assert captured["config"] is d.config
    assert captured["log"] is daemon_module.logger


def test_boot_preflight_fatal_propagates(tmp_path, monkeypatch):
    d = _make_daemon(tmp_path)

    def _raise(config, project_root, log):
        raise RuntimeError("preflight fatal")

    monkeypatch.setattr(
        "src.everbot.cli.milkie_preflight.enforce_boot_preflight", _raise
    )
    with pytest.raises(RuntimeError, match="preflight fatal"):
        d._run_milkie_boot_preflight()
