"""
配置管理
"""

from pathlib import Path
from typing import Dict, Any, Optional
import yaml
import logging

logger = logging.getLogger(__name__)


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
        path = Path("~/.alfred/config.yaml").expanduser()

    if not path.exists():
        logger.warning(f"配置文件不存在: {path}，使用默认配置")
        return get_default_config()

    try:
        with open(path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        logger.info(f"配置已加载: {path}")
        return config
    except Exception as e:
        logger.error(f"加载配置失败: {e}")
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
        path = Path("~/.alfred/config.yaml").expanduser()

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(config, f, default_flow_style=False, allow_unicode=True)
        logger.info(f"配置已保存: {path}")
    except Exception as e:
        logger.error(f"保存配置失败: {e}")
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
