#!/usr/bin/env python3
"""
Live E2E test: send a coding task to the running everbot service.

Prerequisites:
  - everbot daemon running: ./bin/everbot start
  - workspace env0 registered in ~/.alfred/config.yaml with a buggy project
  - env0 is a clean git repo with calculator.py (multiply bug)

Usage:
  python skills/coding-master/tests/test_e2e_live.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import websockets


AGENT = "demo_agent"
WS_URL = f"ws://localhost:8765/ws/chat/{AGENT}"
TIMEOUT = 300  # 5 min max for the full conversation


async def send_and_collect(ws, message: str, wait_for_end: bool = True, timeout: float = 120) -> list[dict]:
    """Send a message and collect all response events until 'end' type."""
    await ws.send(json.dumps({"message": message}))
    events = []
    full_text = ""
    start = time.time()

    while True:
        if time.time() - start > timeout:
            print(f"\n[TIMEOUT] No 'end' event after {timeout}s")
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=60)
            data = json.loads(raw)
            events.append(data)
            t = data.get("type", "")

            if t == "delta":
                content = data.get("content", "")
                full_text += content
                print(content, end="", flush=True)
            elif t == "message":
                content = data.get("content", "")
                if content:
                    print(f"\n[MSG] {content[:200]}")
            elif t == "status":
                content = data.get("content", "")
                if content:
                    print(f"\n[STATUS] {content}")
            elif t == "skill":
                print(f"\n[SKILL] {json.dumps(data, ensure_ascii=False)[:200]}")
            elif t == "end":
                print(f"\n[END] turn completed")
                break
            elif t == "error":
                print(f"\n[ERROR] {data.get('content', '')}")
                break
            elif t == "history":
                n = len(data.get("messages", []))
                print(f"[HISTORY] {n} messages restored")
            elif t == "mailbox_drain":
                n = len(data.get("events", []))
                print(f"[MAILBOX] {n} events drained")
            else:
                print(f"\n[{t.upper()}] {json.dumps(data, ensure_ascii=False)[:150]}")

        except asyncio.TimeoutError:
            print("\n[WAIT] No response for 60s, still waiting...")
            continue

    if full_text:
        print()  # newline after streaming deltas
    return events


async def main():
    print(f"Connecting to {WS_URL} ...")
    async with websockets.connect(
        WS_URL, max_size=10 * 1024 * 1024, proxy=None,
        ping_interval=30, ping_timeout=120,
        close_timeout=10,
    ) as ws:
        # Wait for initial greeting / history
        try:
            init = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(init)
            t = data.get("type", "")
            if t == "history":
                n = len(data.get("messages", []))
                print(f"[INIT] History restored: {n} messages")
            elif t == "message":
                print(f"[INIT] {data.get('content', '')[:100]}")
            else:
                print(f"[INIT] {t}: {json.dumps(data, ensure_ascii=False)[:100]}")
        except asyncio.TimeoutError:
            print("[INIT] No greeting received, proceeding...")

        # Drain any additional init messages (mailbox, etc)
        while True:
            try:
                extra = await asyncio.wait_for(ws.recv(), timeout=2)
                data = json.loads(extra)
                print(f"[INIT-EXTRA] {data.get('type', 'unknown')}")
            except asyncio.TimeoutError:
                break

        print("\n" + "=" * 60)
        print("PHASE 0: Sending coding task to agent")
        print("=" * 60 + "\n")

        # Send the coding task - explicitly mention coding-master skill
        task_msg = (
            "用 coding-master 技能帮我修 env0 workspace 里的 bug。"
            "calculator.py 的 multiply 函数有问题，测试会失败。"
        )
        events = await send_and_collect(ws, task_msg, timeout=180)

        # Check if agent asked for confirmation (Phase 0 behavior)
        full_response = "".join(
            e.get("content", "") for e in events if e.get("type") in ("delta", "message")
        )

        print("\n" + "=" * 60)
        print(f"Agent response length: {len(full_response)} chars")
        print("=" * 60)

        # If agent is waiting for confirmation, send "开始"
        if any(kw in full_response for kw in ["确认", "开始", "proceed", "Proceed", "继续"]):
            print("\n[AUTO] Agent is waiting for confirmation, sending '开始分析并修复'")
            events2 = await send_and_collect(ws, "开始分析并修复", timeout=180)

            full_r2 = "".join(
                e.get("content", "") for e in events2 if e.get("type") in ("delta", "message")
            )

            # Keep following up if more confirmations needed
            for follow_up in ["修吧", "提交", "好的"]:
                if any(kw in full_r2 for kw in ["确认", "继续", "Proceed", "提交", "Submit"]):
                    print(f"\n[AUTO] Sending follow-up: '{follow_up}'")
                    events_n = await send_and_collect(ws, follow_up, timeout=180)
                    full_r2 = "".join(
                        e.get("content", "") for e in events_n if e.get("type") in ("delta", "message")
                    )
                else:
                    break

        print("\n" + "=" * 60)
        print("E2E LIVE TEST COMPLETED")
        print("=" * 60)

        # Final check: look at workspace state
        print("\nChecking workspace state...")
        import subprocess
        repo = "/Users/xupeng/lab/coding_master/env0"

        # Check if lock exists
        import os
        lock_path = os.path.join(repo, ".coding-master.lock")
        if os.path.exists(lock_path):
            lock_data = json.loads(open(lock_path).read())
            print(f"  Lock phase: {lock_data.get('phase', 'unknown')}")
            print(f"  Lock task: {lock_data.get('task', 'unknown')}")
            print(f"  Artifacts: {list(lock_data.get('artifacts', {}).keys())}")
        else:
            print("  No lock file (may have been released)")

        # Check if bug was fixed
        calc_path = os.path.join(repo, "calculator.py")
        if os.path.exists(calc_path):
            code = open(calc_path).read()
            if "a * b" in code:
                print("  calculator.py: BUG FIXED (a * b)")
            else:
                print("  calculator.py: still buggy (a + b)")

        # Check git branch
        r = subprocess.run(["git", "branch", "-a"], cwd=repo, capture_output=True, text=True)
        print(f"  Git branches: {r.stdout.strip()}")

        # Check artifacts dir
        art_dir = os.path.join(repo, ".coding-master")
        if os.path.isdir(art_dir):
            print(f"  Artifacts: {os.listdir(art_dir)}")


if __name__ == "__main__":
    asyncio.run(main())
