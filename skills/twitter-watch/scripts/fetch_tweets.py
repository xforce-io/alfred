#!/usr/bin/env python3
"""抓取指定 X(Twitter)用户的最新推文。

复用同级 ``web`` 技能的 Playwright 浏览器 server(端口 9222)及其**已登录 X** 的
持久化 profile —— 不重造浏览器、不存第二份凭证。实现方式:把一段 Playwright
``tsx`` 脚本经 stdin 喂给 ``web`` 技能目录下的 ``npx tsx``(cwd=web 技能目录,故
``@/client.js`` 子路径导入可解析),抓到的推文以 JSON 打到 stdout。

登录态保护:首屏若只见 "Sign in / Join today"(未登录/cookie 失效),直接以非零
退出码报错,**绝不**伪造内容 —— 对齐 web 技能 SKILL.md 的 Rule 4。

用法:
    python fetch_tweets.py <handle> [--count N]
    python fetch_tweets.py aleabitoreddit --count 3

输出(stdout, JSON):
    {"handle": "...", "count": 3, "tweets": [{"text","ts","url","is_pinned","metrics"}...]}
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# web 技能是同级技能:.../skills/web
WEB_SKILL_DIR = os.path.normpath(os.path.join(SKILL_DIR, "..", "web"))
SERVER_PORT = 9222


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex((host, port)) == 0


def _ensure_browser_server() -> None:
    """确保 web 技能的浏览器 server 在 :9222 监听;不在则拉起并等待就绪。"""
    if _port_open(SERVER_PORT):
        return
    server_sh = os.path.join(WEB_SKILL_DIR, "server.sh")
    if not os.path.isfile(server_sh):
        sys.exit(f"[fetch_tweets] web 技能 server.sh 不存在: {server_sh}")
    log = open(os.path.join(WEB_SKILL_DIR, "server.log"), "ab")
    subprocess.Popen(
        ["bash", server_sh],
        cwd=WEB_SKILL_DIR, stdout=log, stderr=log,
        start_new_session=True,
    )
    for _ in range(60):  # 首次会装 chromium,放宽到 ~120s
        if _port_open(SERVER_PORT):
            time.sleep(1.0)  # 端口起来后给 HTTP API 一点初始化时间
            return
        time.sleep(2.0)
    sys.exit("[fetch_tweets] 浏览器 server 启动超时(:9222 未就绪)")


# 内联 Playwright 抓取脚本(经 stdin 喂给 cwd=web 技能目录的 npx tsx)。
# handle/count 经环境变量注入,避免拼串注入风险。
_TSX = r"""
import { connect, waitForPageLoad } from "@/client.js";

const handle = process.env.TW_HANDLE;
const count = parseInt(process.env.TW_COUNT || "3", 10);

const client = await connect();
const page = await client.page("twitter-watch");
await page.setViewportSize({ width: 1280, height: 2000 });
await page.goto(`https://x.com/${handle}`, { waitUntil: "domcontentloaded" });
await waitForPageLoad(page).catch(() => {});
await page.waitForTimeout(5000);

// 登录态保护:未登录时 X 只渲染登录墙。
const loggedOut = await page
  .locator('text=/Sign in to X|Sign up|Create account|Join today/')
  .count()
  .catch(() => 0);
const handleVisible = await page
  .locator(`text=/@${handle}/i`)
  .count()
  .catch(() => 0);
if (loggedOut > 0 && handleVisible === 0) {
  console.error(
    "LOGIN_REQUIRED: x.com 显示登录墙 — web 技能 profile 未登录或 cookie 已失效," +
    "请在该 profile 里重新登录 X 一次。"
  );
  await client.disconnect();
  process.exit(3);
}

// 渐进滚动,触发懒加载,直到攒够 count 条或到达上限。
const seen = new Map();
for (let scroll = 0; scroll < 8 && seen.size < count; scroll++) {
  const batch = await page.evaluate(() => {
    const arts = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
    return arts.map((a) => {
      const textEl = a.querySelector('[data-testid="tweetText"]');
      const timeEl = a.querySelector('time');
      const linkEl = timeEl ? timeEl.closest('a') : null;
      const social = a.querySelector('[role="group"]');
      const pinned = !!Array.from(a.querySelectorAll('span'))
        .find((e) => /^Pinned/i.test((e.textContent || "").trim()));
      return {
        text: textEl ? textEl.innerText : "",
        ts: timeEl ? timeEl.getAttribute("datetime") : null,
        url: linkEl ? linkEl.href : null,
        is_pinned: pinned,
        metrics: social ? (social.getAttribute("aria-label") || "") : "",
      };
    });
  });
  for (const t of batch) {
    const key = t.url || t.ts || t.text.slice(0, 40);
    if (key && !seen.has(key)) seen.set(key, t);
  }
  await page.evaluate(() => window.scrollBy(0, window.innerHeight * 1.5));
  await page.waitForTimeout(1500);
}

// 置顶推文不计入"最新":按时间倒序取最新 count 条。
let tweets = Array.from(seen.values());
const nonPinned = tweets.filter((t) => !t.is_pinned && t.ts);
nonPinned.sort((a, b) => (a.ts < b.ts ? 1 : -1));
const out = nonPinned.slice(0, count);

console.log(JSON.stringify({ handle, count: out.length, tweets: out }));
await client.disconnect();
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="抓取指定 X 用户的最新推文")
    ap.add_argument("handle", help="X 用户名(不含 @),如 aleabitoreddit")
    ap.add_argument("--count", type=int, default=3, help="抓取最新条数(默认 3)")
    args = ap.parse_args()

    handle = args.handle.lstrip("@")
    _ensure_browser_server()

    env = dict(os.environ, TW_HANDLE=handle, TW_COUNT=str(args.count))
    proc = subprocess.run(
        ["npx", "tsx"],  # 无文件参数 → tsx 从 stdin 读脚本(ESM,同 web 技能用法)
        cwd=WEB_SKILL_DIR, input=_TSX, env=env,
        capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        sys.exit(f"[fetch_tweets] 抓取失败 (exit {proc.returncode})")

    # tsx 的 stdout 末行才是 JSON(前面可能有 npx/loader 噪音)。
    line = next(
        (l for l in reversed(proc.stdout.splitlines()) if l.strip().startswith("{")),
        "",
    )
    if not line:
        sys.stderr.write(proc.stdout)
        sys.exit("[fetch_tweets] 未拿到 JSON 输出")
    data = json.loads(line)
    if not data.get("tweets"):
        sys.exit(f"[fetch_tweets] @{handle} 未抓到推文(账号不存在/受保护/页面结构变化?)")
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
