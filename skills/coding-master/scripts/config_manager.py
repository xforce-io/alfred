#!/usr/bin/env python3
"""CRUD for ~/.alfred/config.yaml coding_master section."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_PATH = Path("~/.alfred/config.yaml").expanduser()


class ConfigManager:
    def __init__(self, config_path: Path | None = None):
        self.config_path = config_path or DEFAULT_CONFIG_PATH
        self._data: dict | None = None

    # ── Read ────────────────────────────────────────────────

    def _load(self) -> dict:
        if self._data is not None:
            return self._data
        if self.config_path.exists():
            self._data = yaml.safe_load(self.config_path.read_text()) or {}
        else:
            self._data = {}
        return self._data

    def _section(self) -> dict:
        return self._load().setdefault("coding_master", {})

    # ── Write ───────────────────────────────────────────────

    def _save(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=self.config_path.parent, suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                yaml.dump(self._data, f, default_flow_style=False, allow_unicode=True)
            os.rename(tmp, self.config_path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ── Public API ──────────────────────────────────────────

    @staticmethod
    def _bucket_key(kind: str) -> str | None:
        """Map kind → config dict key."""
        return {"repo": "repos", "workspace": "workspaces", "env": "envs"}.get(kind)

    def list_all(self) -> dict:
        sec = self._section()
        return {
            "ok": True,
            "data": {
                "repos": sec.get("repos", {}),
                "workspaces": sec.get("workspaces", {}),
                "envs": sec.get("envs", {}),
                "default_engine": sec.get("default_engine", "claude"),
                "max_turns": sec.get("max_turns", 30),
            },
        }

    def add(self, kind: str, name: str, value: str) -> dict:
        """Add repo, workspace, or env in minimal (string) format."""
        bucket_key = self._bucket_key(kind)
        if bucket_key is None:
            return {"ok": False, "error": f"unknown kind: {kind}"}
        bucket = self._section().setdefault(bucket_key, {})
        if name in bucket:
            return {"ok": False, "error": f"{kind} '{name}' already exists"}
        bucket[name] = value
        self._save()
        return {"ok": True, "data": {kind: name, "value": value}}

    def set_field(self, kind: str, name: str, key: str, value: str) -> dict:
        """Set an extended field; auto-upgrade string → dict."""
        bucket_key = self._bucket_key(kind)
        if bucket_key is None:
            return {"ok": False, "error": f"unknown kind: {kind}"}
        bucket = self._section().setdefault(bucket_key, {})
        if name not in bucket:
            return {"ok": False, "error": f"{kind} '{name}' not found"}
        current = bucket[name]
        # auto-upgrade: string → dict
        if isinstance(current, str):
            primary_key = {"repo": "url", "workspace": "path", "env": "connect"}[kind]
            bucket[name] = {primary_key: current}
        bucket[name][key] = value
        self._save()
        return {"ok": True, "data": {kind: name, "config": bucket[name]}}

    def remove(self, kind: str, name: str) -> dict:
        bucket_key = self._bucket_key(kind)
        if bucket_key is None:
            return {"ok": False, "error": f"unknown kind: {kind}"}
        bucket = self._section().get(bucket_key, {})
        if name not in bucket:
            return {"ok": False, "error": f"{kind} '{name}' not found"}
        del bucket[name]
        self._save()
        return {"ok": True, "data": {"removed": name}}

    # ── Helpers for other modules ───────────────────────────

    def get_repo(self, name: str) -> dict | None:
        """Return normalised repo dict: {name, url, default_branch, ...}."""
        repo = self._section().get("repos", {}).get(name)
        if repo is None:
            return None
        if isinstance(repo, str):
            return {"name": name, "url": repo}
        d = dict(repo)
        d["name"] = name
        return d

    def get_workspace(self, name: str) -> dict | None:
        """Return normalised workspace dict: {name, path, ...}."""
        ws = self._section().get("workspaces", {}).get(name)
        if ws is None:
            return None
        if isinstance(ws, str):
            return {"name": name, "path": str(Path(ws).expanduser())}
        d = dict(ws)
        d["name"] = name
        d["path"] = str(Path(d["path"]).expanduser())
        return d

    def get_env(self, name: str) -> dict | None:
        """Return normalised env dict: {name, connect, type, ...}."""
        env = self._section().get("envs", {}).get(name)
        if env is None:
            return None
        if isinstance(env, str):
            connect = env
            extra: dict[str, Any] = {}
        else:
            extra = dict(env)
            connect = extra.pop("connect", "")
        env_type = "ssh" if "@" in connect else "local"
        result: dict[str, Any] = {"name": name, "connect": connect, "type": env_type}
        if env_type == "ssh":
            # user@host:/path
            parts = connect.split(":", 1)
            result["user_host"] = parts[0]
            result["remote_path"] = parts[1] if len(parts) > 1 else "~"
        else:
            result["local_path"] = str(Path(connect).expanduser())
        result.update(extra)
        return result

    def resolve_envs_for_repo(self, repo_name: str) -> list[dict]:
        """Find envs matching a repo by default_env or naming convention.

        Matching rule: repo 'myapp' matches env 'myapp', 'myapp-prod', etc.
        """
        repo = self.get_repo(repo_name)
        if repo is None:
            return []
        # 1. explicit default_env on repo config
        default_env = repo.get("default_env")
        if default_env:
            env = self.get_env(default_env)
            if env:
                return [env]
        # 2. naming convention: repo "myapp" matches env "myapp-*"
        envs = self._section().get("envs", {})
        matched = []
        for env_name in envs:
            if env_name == repo_name or env_name.startswith(f"{repo_name}-"):
                env = self.get_env(env_name)
                if env:
                    suffix = env_name[len(repo_name):].lstrip("-")
                    if suffix in ("prod", "production"):
                        env["tier"] = "prod"
                    elif suffix in ("staging", "stg"):
                        env["tier"] = "staging"
                    elif suffix in ("local", "dev"):
                        env["tier"] = "local"
                    matched.append(env)
        return matched

    # Backward compat alias
    resolve_envs_for_workspace = resolve_envs_for_repo

    def get_default_engine(self) -> str:
        return self._section().get("default_engine", "claude")

    def get_max_turns(self) -> int:
        return self._section().get("max_turns", 30)
