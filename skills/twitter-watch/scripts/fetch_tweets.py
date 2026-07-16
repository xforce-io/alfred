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
    proc = subprocess.run(
        ["bash", server_sh, "start", "--headless"],
        cwd=WEB_SKILL_DIR, capture_output=True, text=True, timeout=150,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        sys.exit(f"[fetch_tweets] 浏览器 server 启动失败: {detail}")
    for _ in range(60):  # 首次会装 chromium,放宽到 ~120s
        if _port_open(SERVER_PORT):
            time.sleep(1.0)  # 端口起来后给 HTTP API 一点初始化时间
            return
        time.sleep(2.0)
    sys.exit("[fetch_tweets] 浏览器 server 启动后未就绪(:9222)")


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

// Progressively scroll to trigger lazy-load. Count only non-pinned tweets
// toward the goal — pinned tweets are excluded from the final result, so
// counting them would cause the loop to exit early and under-collect.
// X DOM has shifted (#153): articles use data-tweet-id + schema.org meta
// (often no <time> / data-testid="tweet"). Keep legacy selectors as fallback.
const seen = new Map();
const usableCount = () =>
  Array.from(seen.values()).filter((t) => !t.is_pinned && (t.ts || t.text)).length;

for (let scroll = 0; scroll < 8 && usableCount() < count; scroll++) {
  const batch = await page.evaluate(() => {
    let arts = Array.from(document.querySelectorAll("article[data-tweet-id]"));
    if (!arts.length) {
      arts = Array.from(
        document.querySelectorAll('article[itemtype*="SocialMediaPosting"]')
      );
    }
    if (!arts.length) {
      arts = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
    }
    return arts.map((a) => {
      const tweetId = a.getAttribute("data-tweet-id") || "";
      // Timestamp: legacy <time datetime>, or schema.org meta content.
      const timeEl = a.querySelector("time");
      let ts = timeEl ? timeEl.getAttribute("datetime") : null;
      if (!ts) {
        const metas = Array.from(a.querySelectorAll("meta[content]"));
        for (const m of metas) {
          const c = m.getAttribute("content") || "";
          if (/^\d{4}-\d{2}-\d{2}T/.test(c)) {
            ts = c;
            break;
          }
        }
      }
      const linkEl =
        (timeEl ? timeEl.closest("a") : null) ||
        a.querySelector('a[href*="/status/"]');
      let url = linkEl ? linkEl.href : null;
      if (!url && tweetId) {
        const handleMatch = (window.location.pathname || "").match(/^\/([^/]+)/);
        const h = handleMatch ? handleMatch[1] : "i";
        url = "https://x.com/" + h + "/status/" + tweetId;
      }
      // Text: prefer classic tweetText; else article innerText (new DOM).
      const textEl = a.querySelector('[data-testid="tweetText"]');
      let text = textEl
        ? (textEl.innerText || textEl.textContent || "").trim()
        : (a.innerText || "").trim();
      // Drop leading author/handle/date chrome when using full article text.
      if (!textEl && text) {
        const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
        const isHandle = (s) => /^@\w+/i.test(s);
        const isDate = (s) =>
          /^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d/i.test(s) ||
          /^\d{1,2}\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)/i.test(s);
        // display name + @handle + date
        if (lines.length >= 3 && isHandle(lines[1]) && isDate(lines[2])) {
          lines.splice(0, 3);
        } else if (lines.length >= 2 && isHandle(lines[0]) && isDate(lines[1])) {
          lines.splice(0, 2);
        }
        text = lines.join("\n").trim();
      }
      const social = a.querySelector('[role="group"]');
      const pinned = !!Array.from(a.querySelectorAll("span")).find((e) =>
        /^(Pinned|已置顶)/i.test((e.textContent || "").trim())
      );
      const truncated = !!(
        a.querySelector('[data-testid="tweet-text-show-more-link"]') ||
        Array.from(a.querySelectorAll("span, a")).find((e) =>
          /show more|显示更多/i.test((e.textContent || "").trim())
        )
      );
      return {
        text,
        ts,
        url,
        is_pinned: pinned,
        metrics: social ? social.getAttribute("aria-label") || "" : "",
        truncated,
      };
    }).filter((t) => t.text || t.url || t.ts);
  });
  for (const t of batch) {
    const key = t.url || t.ts || (t.text || "").slice(0, 40);
    if (key && !seen.has(key)) seen.set(key, t);
  }
  await page.evaluate(() => window.scrollBy(0, window.innerHeight * 1.5));
  await page.waitForTimeout(1500);
}

// Exclude pinned tweets; sort by timestamp descending; take the newest count.
let tweets = Array.from(seen.values());
const nonPinned = tweets.filter((t) => !t.is_pinned && (t.ts || t.text));
nonPinned.sort((a, b) => {
  if (a.ts && b.ts) return a.ts < b.ts ? 1 : -1;
  if (a.ts) return -1;
  if (b.ts) return 1;
  return 0;
});
const out = nonPinned.slice(0, count);

// Expand truncated tweets: navigate to the individual tweet page for full text.
for (const t of out) {
  if (t.truncated && t.url) {
    try {
      await page.goto(t.url, { waitUntil: "domcontentloaded" });
      await waitForPageLoad(page).catch(() => {});
      await page.waitForTimeout(2000);
      const fullText = await page.evaluate(() => {
        const textEl =
          document.querySelector('[data-testid="tweetText"]') ||
          document.querySelector("article");
        return textEl ? (textEl.innerText || textEl.textContent || null) : null;
      });
      if (fullText) t.text = fullText;
    } catch (_) {
      // Navigation failed — keep original truncated text
    }
  }
  delete t.truncated;
}

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
        # Structured failure code for isolated fail-fast (#153). Do not invite
        # the agent to rewrite selectors / shell-debug the scraper.
        sys.exit(
            f"[fetch_tweets] SELECTOR_OR_STRUCTURE_CHANGED: @{handle} 未抓到推文"
            f"(账号不存在/受保护/页面结构变化)。"
            f"Fail-fast: do NOT rewrite the scraper or debug via shell; "
            f"report this error and stop."
        )
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
