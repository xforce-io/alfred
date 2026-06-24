#!/usr/bin/env python3
"""把抓到的推文喂给 claude code(``claude -p`` headless)做深度分析,产出中文报告。

输入:fetch_tweets.py 的 JSON(从 stdin 读,或 --input 指定文件)。
输出:claude 生成的结构化深度分析报告(stdout)。

纯文本分析,不开 --dangerously-skip-permissions(claude 只读 prompt、不动文件)。

用法:
    python fetch_tweets.py aleabitoreddit --count 3 | python analyze.py
    python analyze.py --input tweets.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

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


def run_claude(prompt: str, model: str | None, timeout: int) -> str:
    cmd = ["claude", "-p", prompt]
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        sys.exit("[analyze] 未找到 claude CLI — 确认 claude code 已安装且在 PATH")
    except subprocess.TimeoutExpired:
        sys.exit(f"[analyze] claude 分析超时(>{timeout}s)")
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        sys.exit(f"[analyze] claude 退出码 {proc.returncode}")
    out = proc.stdout.strip()
    if not out:
        sys.exit("[analyze] claude 返回空报告")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="用 claude code 深度分析推文")
    ap.add_argument("--input", help="推文 JSON 文件(默认从 stdin 读)")
    ap.add_argument("--model", default=None, help="指定 claude 模型(默认用 claude 默认)")
    ap.add_argument("--timeout", type=int, default=180, help="claude 超时秒数(默认 180)")
    args = ap.parse_args()

    raw = open(args.input, encoding="utf-8").read() if args.input else sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.exit(f"[analyze] 输入不是合法 JSON: {e}")
    if not data.get("tweets"):
        sys.exit("[analyze] 输入无 tweets,无可分析内容")

    report = run_claude(build_prompt(data), args.model, args.timeout)
    print(report)


if __name__ == "__main__":
    main()
