"""
配置管理
"""

from pathlib import Path
from typing import Dict, Any, Optional
import yaml
import logging

logger = logging.getLogger(__name__)


_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _validate_config(config: Dict[str, Any]) -> None:
    """Validate that the config has the required structure and types."""
    if not isinstance(config, dict):
        raise ValueError("Config must be a dictionary")

    if "everbot" in config:
        everbot = config["everbot"]
        if not isinstance(everbot, dict):
            raise ValueError("'everbot' config section must be a dictionary")
        if "agents" in everbot:
            if not isinstance(everbot["agents"], dict):
                raise ValueError("'everbot.agents' must be a dictionary")
            for name, agent_cfg in everbot["agents"].items():
                if not isinstance(agent_cfg, dict):
                    raise ValueError(f"'everbot.agents.{name}' must be a dictionary")

    if "logging" in config:
        logging_cfg = config["logging"]
        if not isinstance(logging_cfg, dict):
            raise ValueError("'logging' config section must be a dictionary")
        level = logging_cfg.get("level")
        if level is not None and str(level).upper() not in _VALID_LOG_LEVELS:
            raise ValueError(
                f"'logging.level' must be one of {_VALID_LOG_LEVELS}, got '{level}'"
            )
        log_file = logging_cfg.get("file")
        if log_file is not None and not isinstance(log_file, str):
            raise ValueError("'logging.file' must be a string")


def _default_config_path() -> Path:
    """Return the default config.yaml path, respecting ALFRED_HOME."""
    import os
    alfred_home = os.environ.get("ALFRED_HOME")
    if alfred_home:
        return Path(alfred_home).expanduser() / "config.yaml"
    return Path("~/.alfred/config.yaml").expanduser()


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    加载配置文件

    Args:
        config_path: 配置文件路径，如果为 None 则使用默认路径

    Returns:
        配置字典
    """
    if config_path:
        path = Path(config_path).expanduser()
    else:
        path = _default_config_path()

    if not path.exists():
        logger.warning("配置文件不存在: %s，使用默认配置", path)
        return get_default_config()

    try:
        with open(path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        _validate_config(config)
        logger.info("配置已加载: %s", path)
        return config
    except (yaml.YAMLError, ValueError, OSError) as e:
        logger.error("加载配置失败: %s", e)
        return get_default_config()


def get_default_config() -> Dict[str, Any]:
    """
    获取默认配置

    Returns:
        默认配置字典
    """
    return {
        "everbot": {
            "enabled": True,
            "agents": {},
        },
        "logging": {
            "level": "INFO",
            "file": "~/.alfred/logs/everbot.log",
        },
    }


def save_config(config: Dict[str, Any], config_path: Optional[str] = None):
    """
    保存配置文件

    Args:
        config: 配置字典
        config_path: 配置文件路径，如果为 None 则使用默认路径
    """
    if config_path:
        path = Path(config_path).expanduser()
    else:
        path = _default_config_path()

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(config, f, default_flow_style=False, allow_unicode=True)
        logger.info("配置已保存: %s", path)
    except Exception as e:
        logger.error("保存配置失败: %s", e)
        raise


# ---------------------------------------------------------------------------
# Module-level cached config
# ---------------------------------------------------------------------------

_cached_config: Optional[Dict[str, Any]] = None
_cached_config_path: Optional[str] = None


def get_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Return a cached config dict, loading from disk on first call.

    If *config_path* differs from the previously cached path the config is
    reloaded automatically.
    """
    global _cached_config, _cached_config_path
    if _cached_config is None or config_path != _cached_config_path:
        _cached_config = load_config(config_path)
        _cached_config_path = config_path
    return _cached_config


def reload_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Force-reload config from disk and update the cache."""
    global _cached_config, _cached_config_path
    _cached_config = load_config(config_path)
    _cached_config_path = config_path
    return _cached_config


def reset_config_cache() -> None:
    """Clear the cached config (mainly for tests)."""
    global _cached_config, _cached_config_path
    _cached_config = None
    _cached_config_path = None
