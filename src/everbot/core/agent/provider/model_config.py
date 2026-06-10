"""模型路由配置(dolphin-free)。

从 ``models.yaml``(#74 正名;legacy ``dolphin.yaml`` 兜底)读取 llms/clouds + 默认/快速档。
此前 milkie pool 经 dolphin factory 的 ``global_config_path`` 取这份配置,导致 milkie
仍耦合 dolphin;本模块用同样的查找顺序独立定位,去掉该耦合(#38 去 dolphin)。

文件 schema(``config/models.yaml``):
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


# #74:正名 models.yaml(dolphin 已于 #38 移除,旧名误导);同位置内新名优先、
# 旧名兜底 —— 用户 home 级 dolphin.yaml 覆盖仍优先于 cwd/repo 的新名,改名不悄换生效配置。
_CONFIG_NAMES = ("models.yaml", "dolphin.yaml")


def find_model_config_path(
    *,
    home: Optional[Path] = None,
    cwd_config: Optional[Path] = None,
    repo_config: Optional[Path] = None,
) -> Optional[Path]:
    """定位模型配置 yaml。位置优先级:~/.alfred → ./config → repo config/。

    kwargs 仅供测试注入;生产调用零参。
    """
    bases = (
        home or Path("~/.alfred").expanduser(),
        cwd_config or Path("./config").resolve(),
        repo_config or Path(__file__).resolve().parents[5] / "config",  # repo config/
    )
    for base in bases:
        for name in _CONFIG_NAMES:
            p = base / name
            if p.exists():
                return p
    return None


_ENV_PLACEHOLDER = __import__("re").compile(r"\$\{[^}]+\}")


def _expand(v: Any) -> Any:
    """展开 ${ENV};未设的占位符 fail-fast(原 dolphin 行为),避免 literal `${VAR}` 泄漏到请求。"""
    if not isinstance(v, str):
        return v
    expanded = os.path.expandvars(v)
    leftover = _ENV_PLACEHOLDER.search(expanded)
    if leftover:
        raise ValueError(f"model config: 环境变量未设置: {leftover.group(0)}(原值 {v!r})")
    return expanded


@dataclass
class ModelRoute:
    base_url: str
    api_key: str
    model: str
    headers: Dict[str, str] = None  # type: ignore[assignment]
    # #71:随请求透传的 provider 私有参数(如 volcengine ark 的
    # ``thinking: {type: disabled}``),cloud 级与 llm 级合并、llm 覆盖(同 headers)。
    extra_body: Dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.headers is None:
            self.headers = {}
        if self.extra_body is None:
            self.extra_body = {}


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
        """解析指定 llm 名的 {base_url, api_key, model, headers}。未知名 → KeyError。

        base_url 优先 llm 级 ``api``(effective_api 语义)再回退 cloud;headers 合并
        cloud + llm(llm 级覆盖 cloud 级),透传如 kimi 的 ``User-Agent``。
        """
        if model_name not in self.llms:
            raise KeyError(f"model '{model_name}' not in llms config")
        llm = self.llms[model_name]
        cloud = self.clouds[llm["cloud"]]
        base = llm.get("api") or cloud["api"]
        headers = {**(cloud.get("headers") or {}), **(llm.get("headers") or {})}
        extra_body = {**(cloud.get("extra_body") or {}), **(llm.get("extra_body") or {})}
        return ModelRoute(
            base_url=_expand(base).rstrip("/"),
            api_key=_expand(llm.get("api_key") or cloud.get("api_key", "")) or "",
            model=llm["model_name"],
            headers={k: _expand(v) for k, v in headers.items()},
            extra_body=extra_body,
        )


def load_model_config(path: Optional[Path] = None) -> ModelConfig:
    """读取并解析模型配置。找不到文件 → 空配置(调用方按需 fail)。"""
    p = path or find_model_config_path()
    raw: Dict[str, Any] = {}
    if p and p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    llms = raw.get("llms", {}) or {}
    clouds = raw.get("clouds", {}) or {}
    # 兼容两套键:models.yaml 用 default/fast;旧代码用 default_model/fast_llm。
    default_model = raw.get("default_model") or raw.get("default") or next(iter(llms), "")
    fast_model = raw.get("fast_llm") or raw.get("fast") or default_model
    return ModelConfig(llms=llms, clouds=clouds, default_model=default_model, fast_model=fast_model)
