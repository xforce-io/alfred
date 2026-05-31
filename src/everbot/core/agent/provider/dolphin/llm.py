"""Dolphin 一次性 LLM 调用封装（迁自 _extractor_helpers / compressor）。"""
from typing import Any

from dolphin.core.llm.llm_client import LLMClient
from dolphin.core.common.enums import Messages as DolphinMessages, MessageRole


async def call_llm(
    context: Any,
    prompt: str,
    temperature: float = 0.3,
    fast: bool = False,
) -> str:
    """Single user-message LLM call via dolphin's LLMClient.

    ``fast=False``: model = default_model or fast_llm or "deepseek-chat"
        (preserves the memory-extractor model-selection semantics).
    ``fast=True``:  model = fast_llm or "qwen-turbo"
        (preserves the session-compressor model-selection semantics).

    Raises ``RuntimeError`` if dolphin surfaced an error string as content
    (it yields error strings as content when retries are exhausted).
    """
    llm_client = LLMClient(context)
    msgs = DolphinMessages()
    msgs.append_message(MessageRole.USER, prompt)

    config = context.get_config()
    if fast:
        model = getattr(config, "fast_llm", None) or "qwen-turbo"
    else:
        model = (
            getattr(config, "default_model", None)
            or getattr(config, "fast_llm", None)
            or "deepseek-chat"
        )

    result = ""
    async for chunk in llm_client.mf_chat_stream(
        messages=msgs,
        model=model,
        temperature=temperature,
        no_cache=True,
    ):
        result = chunk.get("content") or ""

    stripped = result.strip()
    if stripped.startswith("❌") or stripped.startswith("failed to call LLM"):
        raise RuntimeError(f"LLM call returned error: {stripped[:120]}")
    return stripped
