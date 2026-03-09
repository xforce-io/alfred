"""Health check skill — periodic self-diagnosis with Telegram alerting.

Checks:
1. Telegram Bot API connectivity
2. LLM API availability
3. Heartbeat liveness (last heartbeat not too stale)
4. Session storage health

Alert strategy: critical alerts bypass mailbox and go directly via Telegram
Bot API to ensure delivery even when the session pipeline is broken.
"""

import json
import logging
import os
import resource
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..runtime.skill_context import SkillContext

logger = logging.getLogger(__name__)

_STATE_FILENAME = ".health_check_state.json"

# Thresholds
_HEARTBEAT_STALE_MINUTES = 30  # heartbeat older than this = warning
_ALERT_COOLDOWN_SECONDS = 600  # don't spam alerts


@dataclass
class CheckResult:
    name: str
    ok: bool
    message: str
    critical: bool = False  # critical failures trigger direct Telegram alert


@dataclass
class HealthState:
    last_alert_ts: float = 0.0
    consecutive_failures: int = 0
    last_check_ts: float = 0.0
    last_results: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "last_alert_ts": self.last_alert_ts,
            "consecutive_failures": self.consecutive_failures,
            "last_check_ts": self.last_check_ts,
            "last_results": self.last_results,
        }

    @classmethod
    def load(cls, workspace: Path) -> "HealthState":
        path = workspace / _STATE_FILENAME
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                last_alert_ts=float(data.get("last_alert_ts", 0)),
                consecutive_failures=int(data.get("consecutive_failures", 0)),
                last_check_ts=float(data.get("last_check_ts", 0)),
                last_results=data.get("last_results", []),
            )
        except Exception:
            return cls()

    def save(self, workspace: Path) -> None:
        path = workspace / _STATE_FILENAME
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


async def run(context: SkillContext) -> str:
    """Execute health checks and alert on critical failures."""
    state = HealthState.load(context.workspace_path)
    results: List[CheckResult] = []

    # 1. Telegram Bot API check
    results.append(await _check_telegram())

    # 2. LLM API check
    results.append(await _check_llm(context))

    # 3. Heartbeat liveness
    results.append(_check_heartbeat_liveness(context))

    # 4. Session storage health
    results.append(_check_session_storage(context))

    # 5. Process resource usage
    results.append(_check_process_resources())

    # Evaluate results
    failures = [r for r in results if not r.ok]
    critical_failures = [r for r in failures if r.critical]

    state.last_check_ts = time.time()
    state.last_results = [
        {"name": r.name, "ok": r.ok, "message": r.message} for r in results
    ]

    if failures:
        state.consecutive_failures += 1
    else:
        state.consecutive_failures = 0

    # Direct Telegram alert for critical failures (bypass mailbox)
    if critical_failures and _should_alert(state):
        alert_text = _build_alert_text(critical_failures, state)
        sent = await _send_telegram_alert(alert_text)
        if sent:
            state.last_alert_ts = time.time()
            logger.info("Health check alert sent via Telegram")
        else:
            logger.warning("Failed to send health check alert")

    # Non-critical failures: deposit to mailbox
    non_critical_failures = [r for r in failures if not r.critical]
    if non_critical_failures:
        summary = f"Health check: {len(non_critical_failures)} warning(s)"
        detail = "\n".join(f"- {r.name}: {r.message}" for r in non_critical_failures)
        await context.mailbox.deposit(summary, detail)

    state.save(context.workspace_path)

    # Build return summary
    ok_count = sum(1 for r in results if r.ok)
    total = len(results)
    if not failures:
        return f"Health OK ({ok_count}/{total} checks passed)"
    return f"Health WARN: {len(failures)}/{total} failed — " + "; ".join(
        f"{r.name}: {r.message}" for r in failures
    )


# ── Individual checks ─────────────────────────────────────────


async def _check_telegram() -> CheckResult:
    """Check Telegram Bot API connectivity."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        return CheckResult(
            name="telegram",
            ok=False,
            message="TELEGRAM_BOT_TOKEN not set",
            critical=False,  # can't alert via Telegram if no token
        )
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"https://api.telegram.org/bot{bot_token}/getMe")
            if resp.status_code == 200:
                return CheckResult(name="telegram", ok=True, message="connected")
            return CheckResult(
                name="telegram",
                ok=False,
                message=f"HTTP {resp.status_code}",
                critical=True,
            )
    except Exception as e:
        return CheckResult(
            name="telegram",
            ok=False,
            message=f"connection failed: {e}",
            critical=True,
        )


async def _check_llm(context: SkillContext) -> CheckResult:
    """Check LLM API availability with a minimal request."""
    try:
        response = await context.llm.complete("Reply with OK", system="Reply with exactly 'OK'")
        if response and len(response.strip()) > 0:
            return CheckResult(name="llm", ok=True, message="responsive")
        return CheckResult(
            name="llm",
            ok=False,
            message="empty response",
            critical=True,
        )
    except Exception as e:
        return CheckResult(
            name="llm",
            ok=False,
            message=f"API error: {e}",
            critical=True,
        )


def _check_heartbeat_liveness(context: SkillContext) -> CheckResult:
    """Check if last heartbeat is not too stale."""
    from ...infra.user_data import get_user_data_manager

    user_data = get_user_data_manager()
    status_file = user_data.status_file
    if not status_file.exists():
        return CheckResult(
            name="heartbeat",
            ok=False,
            message="no status file",
            critical=False,
        )
    try:
        status = json.loads(status_file.read_text(encoding="utf-8"))
        heartbeats = status.get("heartbeats", {})
        if not heartbeats:
            return CheckResult(name="heartbeat", ok=True, message="no heartbeats configured")

        for agent_name, hb in heartbeats.items():
            ts_str = hb.get("timestamp", "")
            if not ts_str:
                continue
            last_ts = datetime.fromisoformat(ts_str)
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - last_ts
            if age > timedelta(minutes=_HEARTBEAT_STALE_MINUTES):
                return CheckResult(
                    name="heartbeat",
                    ok=False,
                    message=f"{agent_name} last heartbeat {int(age.total_seconds() // 60)}min ago",
                    critical=False,
                )
        return CheckResult(name="heartbeat", ok=True, message="recent")
    except Exception as e:
        return CheckResult(name="heartbeat", ok=False, message=str(e), critical=False)


def _check_session_storage(context: SkillContext) -> CheckResult:
    """Check session directory is accessible and not full."""
    try:
        if not context.sessions_dir.exists():
            return CheckResult(
                name="sessions",
                ok=False,
                message="sessions directory missing",
                critical=True,
            )
        # Check disk space via statvfs
        stat = os.statvfs(str(context.sessions_dir))
        free_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
        if free_mb < 100:
            return CheckResult(
                name="sessions",
                ok=False,
                message=f"low disk space: {free_mb:.0f}MB free",
                critical=True,
            )
        # Count session files
        session_count = sum(1 for _ in context.sessions_dir.glob("*.json"))
        return CheckResult(
            name="sessions",
            ok=True,
            message=f"{session_count} sessions, {free_mb:.0f}MB free",
        )
    except Exception as e:
        return CheckResult(name="sessions", ok=False, message=str(e), critical=False)


def _check_process_resources() -> CheckResult:
    """Check process memory and uptime."""
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # maxrss is in bytes on Linux, kilobytes on macOS
        import sys
        if sys.platform == "darwin":
            rss_mb = usage.ru_maxrss / (1024 * 1024)
        else:
            rss_mb = usage.ru_maxrss / 1024

        pid = os.getpid()
        # Process uptime via /proc on Linux, or ps on macOS
        uptime_str = "unknown"
        try:
            if sys.platform == "darwin":
                import subprocess
                result = subprocess.run(
                    ["ps", "-o", "etime=", "-p", str(pid)],
                    capture_output=True, text=True, timeout=5,
                )
                uptime_str = result.stdout.strip() if result.returncode == 0 else "unknown"
            else:
                stat_path = Path(f"/proc/{pid}/stat")
                if stat_path.exists():
                    # Use clock ticks from /proc/stat
                    boot_ticks = int(stat_path.read_text().split(")")[1].split()[19])
                    clk_tck = os.sysconf("SC_CLK_TCK")
                    uptime_secs = time.time() - boot_ticks / clk_tck
                    hours, remainder = divmod(int(uptime_secs), 3600)
                    mins, _ = divmod(remainder, 60)
                    uptime_str = f"{hours}h{mins}m"
        except Exception:
            pass

        # Warn if RSS > 512MB
        if rss_mb > 512:
            return CheckResult(
                name="process",
                ok=False,
                message=f"high memory: {rss_mb:.0f}MB RSS, pid={pid}, uptime={uptime_str}",
                critical=False,
            )
        return CheckResult(
            name="process",
            ok=True,
            message=f"{rss_mb:.0f}MB RSS, pid={pid}, uptime={uptime_str}",
        )
    except Exception as e:
        return CheckResult(name="process", ok=False, message=str(e), critical=False)


# ── Alerting ──────────────────────────────────────────────────


def _should_alert(state: HealthState) -> bool:
    """Respect cooldown to avoid alert spam."""
    return (time.time() - state.last_alert_ts) >= _ALERT_COOLDOWN_SECONDS


def _build_alert_text(failures: List[CheckResult], state: HealthState) -> str:
    hostname = os.uname().nodename
    lines = [f"⚠️ EverBot Health Alert ({hostname})"]
    for f in failures:
        lines.append(f"• {f.name}: {f.message}")
    if state.consecutive_failures > 1:
        lines.append(f"Consecutive failures: {state.consecutive_failures}")
    return "\n".join(lines)


async def _send_telegram_alert(text: str) -> bool:
    """Send alert directly via Telegram Bot API, bypassing session pipeline."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("EVERBOT_ALERT_CHAT_ID", "")
    if not bot_token or not chat_id:
        logger.warning("Cannot send Telegram alert: missing BOT_TOKEN or ALERT_CHAT_ID")
        return False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
            return resp.status_code == 200
    except Exception as e:
        logger.error("Telegram alert send failed: %s", e)
        return False
