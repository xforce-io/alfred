"""模型路由配置(dolphin-free)。

从 ``config/dolphin.yaml``(纯 YAML,非 dolphin 代码)读取 llms/clouds + 默认/快速档。
此前 milkie pool 经 dolphin factory 的 ``global_config_path`` 取这份配置,导致 milkie
仍耦合 dolphin;本模块用同样的查找顺序独立定位,去掉该耦合(#38 去 dolphin)。

文件 schema(``config/dolphin.yaml``):
    default: <llm-name>        # 默认档(亦兼容旧键 default_model)
    fast:    <llm-name>        # 快速档(亦兼容旧键 fast_llm)
    clouds:  {name: {api, api_key}}
    llms:    {name: {cloud, model_name, type_api}}
``api``/``api_key`` 可含 ``${ENV}`` 占位符,读取时按环境变量展开。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


def find_model_config_path() -> Optional[Path]:
    """定位模型配置 yaml(同 dolphin 的查找顺序,但不依赖 dolphin)。"""
    candidates = [
        Path("~/.alfred/dolphin.yaml").expanduser(),
        Path("./config/dolphin.yaml").resolve(),
        Path(__file__).resolve().parents[5] / "config" / "dolphin.yaml",  # repo config/
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _expand(v: Any) -> Any:
    return os.path.expandvars(v) if isinstance(v, str) else v


@dataclass
class ModelRoute:
    base_url: str
    api_key: str
    model: str


@dataclass
class ModelConfig:
    llms: Dict[str, Any]
    clouds: Dict[str, Any]
    default_model: str
    fast_model: str

    def route(self, *, fast: bool = False) -> ModelRoute:
        """解析默认/快速档的 {base_url, api_key, model}(env 占位符已展开)。"""
        return self.route_for(self.fast_model if fast else self.default_model)

    def route_for(self, model_name: str) -> ModelRoute:
        """解析指定 llm 名的 {base_url, api_key, model}。未知名 → KeyError。"""
        if model_name not in self.llms:
            raise KeyError(f"model '{model_name}' not in llms config")
        llm = self.llms[model_name]
        cloud = self.clouds[llm["cloud"]]
        return ModelRoute(
            base_url=_expand(cloud["api"]).rstrip("/"),
            api_key=_expand(cloud.get("api_key", "")) or "",
            model=llm["model_name"],
        )


def load_model_config(path: Optional[Path] = None) -> ModelConfig:
    """读取并解析模型配置。找不到文件 → 空配置(调用方按需 fail)。"""
    p = path or find_model_config_path()
    raw: Dict[str, Any] = {}
    if p and p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    llms = raw.get("llms", {}) or {}
    clouds = raw.get("clouds", {}) or {}
    # 兼容两套键:config/dolphin.yaml 用 default/fast;旧代码用 default_model/fast_llm。
    default_model = raw.get("default_model") or raw.get("default") or next(iter(llms), "")
    fast_model = raw.get("fast_llm") or raw.get("fast") or default_model
    return ModelConfig(llms=llms, clouds=clouds, default_model=default_model, fast_model=fast_model)
