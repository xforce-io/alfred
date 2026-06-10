"""一次性无状态 LLM(dolphin-free)—— 记忆抽取 / 历史压缩用。

``call_llm`` 与 agent runtime 无关(单 prompt → 单回复),此前经 dolphin 的 LLMClient
实现。本模块改为直连 OpenAI 兼容端点(httpx),模型路由读 ``config/models.yaml``
(见 :mod:`model_config`),去掉对 dolphin 的依赖(#38)。

``raise_on_error`` 双语义保持与原 DolphinProvider.call_llm 一致:
- True(默认,记忆抽取):出错抛 ``RuntimeError``;
- False(历史压缩):出错把错误串作为结果返回,调用方优雅降级。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .model_config import load_model_config

logger = logging.getLogger(__name__)


class OneshotLLMProvider:
    """无状态一次性 LLM provider(只实现 ``call_llm``)。"""

    def __init__(self, *, client: "httpx.AsyncClient | None" = None) -> None:
        self._client = client  # 注入便于测试;None → 每次调用一个 client

    async def call_llm(
        self,
        context: Any,
        prompt: str,
        temperature: float = 0.3,
        fast: bool = False,
        raise_on_error: bool = True,
    ) -> str:
        try:
            route = load_model_config().route(fast=fast)
        except Exception as exc:  # 配置缺失/模型名不存在
            msg = f"oneshot LLM 配置错误: {exc}"
            if raise_on_error:
                raise RuntimeError(msg) from exc
            return msg

        payload = {
            "model": route.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {route.api_key}",
            "content-type": "application/json",
            **route.headers,  # 透传 cloud 级 headers(如 kimi User-Agent)
        }
        # base_url 可能已含 /chat/completions(防双拼)
        base = route.base_url
        url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(120.0), trust_env=False)
        owns = self._client is None
        try:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                msg = f"oneshot LLM HTTP {resp.status_code}: {resp.text[:300]}"
                if raise_on_error:
                    raise RuntimeError(msg)
                return msg
            data = resp.json()
            return (data["choices"][0]["message"]["content"] or "").strip()
        except RuntimeError:
            raise
        except Exception as exc:
            msg = f"oneshot LLM 调用失败: {exc}"
            if raise_on_error:
                raise RuntimeError(msg) from exc
            return msg
        finally:
            if owns:
                await client.aclose()
