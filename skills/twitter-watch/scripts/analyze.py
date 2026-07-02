#!/usr/bin/env python3
"""Analyze fetched tweets with the configured OpenAI-compatible model route.

输入:fetch_tweets.py 的 JSON(从 stdin 读,或 --input 指定文件)。
输出:模型生成的结构化深度分析报告(stdout)。

用法:
    python fetch_tweets.py aleabitoreddit --count 3 | python analyze.py
    python analyze.py --input tweets.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.everbot.core.agent.provider.model_config import load_model_config  # noqa: E402

_PROMPT_TEMPLATE = """你是资深投资与科技分析师。下面是 X 用户 @{handle} 最新的 {n} 条推文(含互动数据 metrics)。请做**深度分析**,用**中文**输出结构化报告。要求基于推文内容分析,保留关键原文引用,**不要编造推文未提及的信息**。

## 逐条解读
对每条推文:核心观点(1-2 句)+ 涉及的标的/资产(如 $XXX)+ 信号类型(看多/看空/中性/纯信息)。
**每条务必附上该推文的原文链接**(用数据里的 `url` 字段),便于溯源核实;`url` 为空则注明"无链接"。

## 投资信号与标的
汇总提到的股票/资产,逐个给出:作者的多空倾向、其依据、潜在交易机会与风险点。

## 整体主题与趋势
这批推文的共同主题、叙事/观点变化、值得持续跟踪的线索。

## 作者立场与情绪
作者整体的观点倾向、确信度、可能的偏见或争议点。

---
推文数据(JSON):
{tweets_json}
"""


def build_prompt(data: dict) -> str:
    handle = data.get("handle", "?")
    tweets = data.get("tweets", [])
    return _PROMPT_TEMPLATE.format(
        handle=handle,
        n=len(tweets),
        tweets_json=json.dumps(data, ensure_ascii=False, indent=2),
    )


async def run_analysis(prompt: str, model: str | None, timeout: int, fast: bool = False) -> str:
    try:
        cfg = load_model_config()
        route = cfg.route_for(model) if model else cfg.route(fast=fast)
    except Exception as exc:
        sys.exit(f"[analyze] 模型配置错误: {exc}")

    payload: dict[str, Any] = {
        "model": route.model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "stream": False,
        **route.extra_body,
    }
    headers = {
        "Authorization": f"Bearer {route.api_key}",
        "content-type": "application/json",
        **route.headers,
    }
    base = route.base_url
    url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(float(timeout)), trust_env=False) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException:
        sys.exit(f"[analyze] 模型分析超时(>{timeout}s)")
    except Exception as exc:
        sys.exit(f"[analyze] 模型调用失败: {exc}")

    if resp.status_code >= 400:
        sys.stderr.write(resp.text)
        sys.exit(f"[analyze] 模型接口 HTTP {resp.status_code}")

    try:
        data = resp.json()
        out = (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:
        sys.exit(f"[analyze] 模型返回格式无效: {exc}")
    if not out:
        sys.exit("[analyze] 模型返回空报告")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="用已配置模型深度分析推文")
    ap.add_argument("--input", help="推文 JSON 文件(默认从 stdin 读)")
    ap.add_argument("--model", default=None, help="指定 config/models.yaml 里的 llm 名称(默认用 default)")
    ap.add_argument("--fast", action="store_true", help="未指定 --model 时使用 fast 档")
    ap.add_argument("--timeout", type=int, default=180, help="模型调用超时秒数(默认 180)")
    args = ap.parse_args()

    raw = open(args.input, encoding="utf-8").read() if args.input else sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.exit(f"[analyze] 输入不是合法 JSON: {e}")
    if not data.get("tweets"):
        sys.exit("[analyze] 输入无 tweets,无可分析内容")

    report = asyncio.run(run_analysis(build_prompt(data), args.model, args.timeout, fast=args.fast))
    print(report)


if __name__ == "__main__":
    main()
