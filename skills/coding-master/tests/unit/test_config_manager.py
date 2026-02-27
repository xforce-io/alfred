"""Tests for config_manager.py — ConfigManager CRUD."""

import pytest
import yaml

from config_manager import ConfigManager


class TestListAll:
    def test_empty_config(self, tmp_path):
        cfg_path = tmp_path / "empty.yaml"
        cm = ConfigManager(config_path=cfg_path)
        result = cm.list_all()
        assert result["ok"] is True
        assert result["data"]["workspaces"] == {}
        assert result["data"]["envs"] == {}
        assert result["data"]["default_engine"] == "claude"
        assert result["data"]["max_turns"] == 30

    def test_with_data(self, config_manager):
        result = config_manager.list_all()
        assert result["ok"] is True
        assert "test-ws" in result["data"]["workspaces"]


class TestAdd:
    def test_add_workspace(self, tmp_path):
        cm = ConfigManager(config_path=tmp_path / "cfg.yaml")
        result = cm.add("workspace", "myws", "/tmp/myws")
        assert result["ok"] is True
        assert result["data"]["workspace"] == "myws"
        # Verify persisted
        cm2 = ConfigManager(config_path=tmp_path / "cfg.yaml")
        assert cm2.list_all()["data"]["workspaces"]["myws"] == "/tmp/myws"

    def test_add_env(self, tmp_path):
        cm = ConfigManager(config_path=tmp_path / "cfg.yaml")
        result = cm.add("env", "myenv", "user@host:/path")
        assert result["ok"] is True
        assert result["data"]["env"] == "myenv"

    def test_add_duplicate_fails(self, config_manager):
        result = config_manager.add("workspace", "test-ws", "/other")
        assert result["ok"] is False
        assert "already exists" in result["error"]

    def test_add_unknown_kind(self, tmp_path):
        cm = ConfigManager(config_path=tmp_path / "cfg.yaml")
        result = cm.add("unknown", "x", "y")
        assert result["ok"] is False


class TestSetField:
    def test_auto_upgrade_string_to_dict(self, tmp_path):
        cm = ConfigManager(config_path=tmp_path / "cfg.yaml")
        cm.add("workspace", "ws1", "/path/to/ws")
        result = cm.set_field("workspace", "ws1", "test_command", "pytest")
        assert result["ok"] is True
        cfg = result["data"]["config"]
        assert cfg["path"] == "/path/to/ws"
        assert cfg["test_command"] == "pytest"

    def test_auto_upgrade_env_uses_connect_key(self, tmp_path):
        cm = ConfigManager(config_path=tmp_path / "cfg.yaml")
        cm.add("env", "e1", "user@host:/path")
        result = cm.set_field("env", "e1", "log", "/var/log/app.log")
        assert result["ok"] is True
        cfg = result["data"]["config"]
        assert cfg["connect"] == "user@host:/path"
        assert cfg["log"] == "/var/log/app.log"

    def test_set_field_not_found(self, tmp_path):
        cm = ConfigManager(config_path=tmp_path / "cfg.yaml")
        result = cm.set_field("workspace", "nope", "k", "v")
        assert result["ok"] is False
        assert "not found" in result["error"]


class TestRemove:
    def test_remove_workspace(self, config_manager):
        result = config_manager.remove("workspace", "test-ws")
        assert result["ok"] is True
        assert config_manager.get_workspace("test-ws") is None

    def test_remove_not_found(self, config_manager):
        result = config_manager.remove("workspace", "nope")
        assert result["ok"] is False


class TestGetWorkspace:
    def test_string_format_expands_tilde(self, tmp_path):
        cm = ConfigManager(config_path=tmp_path / "cfg.yaml")
        cm.add("workspace", "home", "~/projects/foo")
        ws = cm.get_workspace("home")
        assert ws is not None
        assert "~" not in ws["path"]
        assert ws["name"] == "home"

    def test_dict_format(self, tmp_path):
        cfg_path = tmp_path / "cfg.yaml"
        data = {
            "coding_master": {
                "workspaces": {
                    "ws1": {"path": "/abs/path", "test_command": "pytest"},
                }
            }
        }
        cfg_path.write_text(yaml.dump(data))
        cm = ConfigManager(config_path=cfg_path)
        ws = cm.get_workspace("ws1")
        assert ws["path"] == "/abs/path"
        assert ws["test_command"] == "pytest"

    def test_not_found(self, tmp_path):
        cm = ConfigManager(config_path=tmp_path / "cfg.yaml")
        assert cm.get_workspace("x") is None


class TestGetEnv:
    def test_ssh_env(self, tmp_path):
        cm = ConfigManager(config_path=tmp_path / "cfg.yaml")
        cm.add("env", "prod", "deploy@10.0.0.1:/app")
        env = cm.get_env("prod")
        assert env["type"] == "ssh"
        assert env["user_host"] == "deploy@10.0.0.1"
        assert env["remote_path"] == "/app"

    def test_local_env(self, tmp_path):
        cm = ConfigManager(config_path=tmp_path / "cfg.yaml")
        cm.add("env", "local", "/var/app")
        env = cm.get_env("local")
        assert env["type"] == "local"
        assert env["local_path"] == "/var/app"

    def test_not_found(self, tmp_path):
        cm = ConfigManager(config_path=tmp_path / "cfg.yaml")
        assert cm.get_env("x") is None


class TestResolveEnvs:
    def test_naming_convention(self, tmp_path):
        cfg_path = tmp_path / "cfg.yaml"
        data = {
            "coding_master": {
                "workspaces": {"alfred": "/path/alfred"},
                "repos": {"alfred": "/path/alfred"},
                "envs": {
                    "alfred-prod": "deploy@prod:/app",
                    "alfred-staging": "deploy@stg:/app",
                    "other": "/other",
                },
            }
        }
        cfg_path.write_text(yaml.dump(data))
        cm = ConfigManager(config_path=cfg_path)
        envs = cm.resolve_envs_for_workspace("alfred")
        names = {e["name"] for e in envs}
        assert "alfred-prod" in names
        assert "alfred-staging" in names
        assert "other" not in names

    def test_explicit_default_env(self, tmp_path):
        cfg_path = tmp_path / "cfg.yaml"
        data = {
            "coding_master": {
                "workspaces": {"ws": {"path": "/ws", "default_env": "myenv"}},
                "repos": {"ws": {"url": "/ws", "default_env": "myenv"}},
                "envs": {"myenv": "/env/path"},
            }
        }
        cfg_path.write_text(yaml.dump(data))
        cm = ConfigManager(config_path=cfg_path)
        envs = cm.resolve_envs_for_workspace("ws")
        assert len(envs) == 1
        assert envs[0]["name"] == "myenv"

    def test_tier_inference(self, tmp_path):
        cfg_path = tmp_path / "cfg.yaml"
        data = {
            "coding_master": {
                "workspaces": {"app": "/app"},
                "repos": {"app": "/app"},
                "envs": {
                    "app-prod": "u@h:/p",
                    "app-local": "/local",
                },
            }
        }
        cfg_path.write_text(yaml.dump(data))
        cm = ConfigManager(config_path=cfg_path)
        envs = cm.resolve_envs_for_workspace("app")
        tiers = {e["name"]: e.get("tier") for e in envs}
        assert tiers["app-prod"] == "prod"
        assert tiers["app-local"] == "local"
