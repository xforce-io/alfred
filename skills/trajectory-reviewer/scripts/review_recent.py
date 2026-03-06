#!/usr/bin/env python3
"""Review recent trajectory files and logs to surface actionable issues."""

from __future__ import annotations

import argparse
import hashlib
import glob
import json
import re
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

DEFAULT_TRAJECTORY_GLOB = "~/.alfred/agents/*/tmp/trajectory_*.json"
DEFAULT_LOG_CANDIDATES = [
    Path("log/dolphin.log"),
    Path("~/.alfred/logs/everbot.log").expanduser(),
    Path("~/.alfred/logs/heartbeat.log").expanduser(),
]

ERROR_HINT_PATTERNS: Sequence[Tuple[re.Pattern[str], str]] = (
    (re.compile(r"timeout", re.IGNORECASE), "Tune timeout, split long jobs, or move heavy tasks to isolated mode."),
    (re.compile(r"429|rate limit", re.IGNORECASE), "Add backoff and request throttling for external APIs."),
    (re.compile(r"name '.*' is not defined", re.IGNORECASE), "Validate generated code imports before execution."),
    (re.compile(r"file not found|no such file", re.IGNORECASE), "Add path existence checks before tool calls."),
)


@dataclass
class Finding:
    severity: str
    title: str
    evidence: List[str]
    recommendation: str


@dataclass
class LoadedTrajectory:
    path: Path
    payload: Dict[str, Any]
    error: Optional[str] = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review recent trajectory and log files.")
    parser.add_argument("--agent", default=None, help="Filter trajectory by agent name.")
    parser.add_argument("--session", default=None, help="Filter trajectory by session id substring.")
    parser.add_argument("--limit-files", type=int, default=2, help="Number of latest trajectory files to analyze.")
    parser.add_argument("--tail-lines", type=int, default=3000, help="Number of recent log lines to analyze.")
    parser.add_argument("--trajectory-glob", default=DEFAULT_TRAJECTORY_GLOB, help="Glob pattern for trajectory files.")
    parser.add_argument("--dolphin-log", default=None, help="Path to dolphin log file.")
    parser.add_argument("--output", default=None, help="Optional path to write markdown report.")
    return parser.parse_args()


def parse_iso_time(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def extract_session_id(path: Path) -> str:
    name = path.name
    match = re.match(r"trajectory_(.+?)\.json$", name)
    if not match:
        return "unknown"
    raw = match.group(1)
    ts_suffix = re.search(r"\.(\d{8}_\d{6})$", raw)
    if ts_suffix:
        return raw[: -(len(ts_suffix.group(0)))]
    return raw


def extract_agent_name(path: Path) -> Optional[str]:
    parts = path.parts
    try:
        idx = parts.index("agents")
    except ValueError:
        return None
    agent_idx = idx + 1
    if agent_idx >= len(parts):
        return None
    return parts[agent_idx]


def _trajectory_priority(path: Path, anchor_agent: Optional[str], anchor_session: Optional[str]) -> Tuple[int, float]:
    session_id = extract_session_id(path)
    agent_name = extract_agent_name(path)
    mtime = path.stat().st_mtime

    if anchor_session and session_id == anchor_session:
        return (0, -mtime)
    if anchor_agent and session_id == f"heartbeat_session_{anchor_agent}":
        return (1, -mtime)
    if anchor_agent and agent_name == anchor_agent and not session_id.startswith("heartbeat_session_"):
        return (2, -mtime)
    if session_id.startswith("heartbeat_session_"):
        return (4, -mtime)
    return (3, -mtime)


def _session_anchor_rank(session_id: str) -> int:
    if session_id.startswith(("tg_session_", "web_session_", "api_session_", "cli_session_")):
        return 0
    if session_id.startswith("heartbeat_session_"):
        return 2
    if session_id.startswith(("job_", "routine_")):
        return 3
    return 1


def discover_trajectory_files(pattern: str, agent: Optional[str], session: Optional[str], limit: int) -> List[Path]:
    # Use glob with explicit home expansion because the default pattern starts with "~".
    expanded_pattern = str(Path(pattern).expanduser())
    paths = [Path(p) for p in glob.glob(expanded_pattern)]

    filtered: List[Path] = []
    for path in paths:
        if not path.is_file():
            continue
        path_agent = extract_agent_name(path)
        if agent and path_agent != agent:
            continue
        if session and session not in path.name:
            continue
        filtered.append(path)

    filtered.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if not filtered:
        return []

    anchor_path = min(
        filtered,
        key=lambda p: (_session_anchor_rank(extract_session_id(p)), -p.stat().st_mtime),
    )
    anchor_agent = agent or extract_agent_name(anchor_path)
    anchor_session = session or extract_session_id(anchor_path)
    filtered.sort(key=lambda p: _trajectory_priority(p, anchor_agent, anchor_session))
    return filtered[: max(1, limit)]


def load_json(path: Path) -> LoadedTrajectory:
    try:
        return LoadedTrajectory(path=path, payload=json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        return LoadedTrajectory(path=path, payload={}, error=str(exc))


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " ".join(normalize_text(v) for v in value).strip()
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def pick_log_path(explicit_path: Optional[str]) -> Optional[Path]:
    if explicit_path:
        path = Path(explicit_path).expanduser()
        return path if path.exists() else None
    for candidate in DEFAULT_LOG_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def tail_lines(path: Path, count: int) -> List[str]:
    if count <= 0:
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()

    window: deque[str] = deque(maxlen=count)
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            window.append(line.rstrip("\n"))
    return list(window)


def infer_hint(message: str) -> str:
    for pattern, hint in ERROR_HINT_PATTERNS:
        if pattern.search(message):
            return hint
    return "Inspect stack traces and add guardrails for this failure path."


_TOOL_ERROR_PATTERNS: Sequence[re.Pattern[str]] = (
    re.compile(r"(?m)^\s*Command exited with code\s+[1-9]\d*\b"),
    re.compile(r"(?m)^\s*Traceback \(most recent call last\):"),
    re.compile(r"(?m)^\s*\w*Error:"),
    re.compile(r"(?m)^\s*Failed to\b", re.IGNORECASE),
    re.compile(r"(?m)^\s*Exception:"),
)


def extract_tool_error_signature(content_text: str) -> Optional[str]:
    if not content_text:
        return None
    for pattern in _TOOL_ERROR_PATTERNS:
        match = pattern.search(content_text)
        if not match:
            continue
        line = content_text[match.start():].split("\n", 1)[0].strip()
        normalized = re.sub(r"\s+", " ", line)
        if normalized.startswith("Command exited with code"):
            return normalized
        digest = hashlib.md5(normalized.encode("utf-8")).hexdigest()[:12]
        return f"{normalized[:120]}::{digest}"
    return None


def analyze_trajectories(paths: Sequence[Path]) -> Tuple[Dict[str, Any], List[Finding], List[str]]:
    metrics: Dict[str, Any] = {
        "files": len(paths),
        "messages": 0,
        "assistant": 0,
        "tool": 0,
        "tool_call_requests": 0,
        "tool_error_messages": 0,
        "empty_assistant_messages": 0,
        "long_gaps_over_60s": 0,
    }
    findings: List[Finding] = []
    analyzed_sessions: List[str] = []
    repeated_error_counter: Counter[str] = Counter()
    loop_sessions: Dict[str, int] = defaultdict(int)

    for path in paths:
        loaded = load_json(path)
        if loaded.error:
            findings.append(
                Finding(
                    severity="Medium",
                    title="Unreadable trajectory file",
                    evidence=[f"file={path}", f"error={loaded.error[:220]}"],
                    recommendation="Repair or regenerate the trajectory file before relying on this review.",
                )
            )
        payload = loaded.payload
        messages = payload.get("trajectory") if isinstance(payload, dict) else None
        if not isinstance(messages, list):
            continue

        session_id = extract_session_id(path)
        analyzed_sessions.append(session_id)

        prev_assistant_text = ""
        prev_ts: Optional[datetime] = None

        for message in messages:
            if not isinstance(message, dict):
                continue
            metrics["messages"] += 1

            role = str(message.get("role", ""))
            content_text = normalize_text(message.get("content", ""))
            timestamp = parse_iso_time(str(message.get("timestamp", "")))

            if timestamp and prev_ts:
                gap = (timestamp - prev_ts).total_seconds()
                if gap > 60:
                    metrics["long_gaps_over_60s"] += 1
            if timestamp:
                prev_ts = timestamp

            if role == "assistant":
                metrics["assistant"] += 1
                if not content_text and not message.get("tool_calls"):
                    metrics["empty_assistant_messages"] += 1

                if content_text and content_text == prev_assistant_text:
                    loop_sessions[session_id] += 1
                prev_assistant_text = content_text or prev_assistant_text

                tool_calls = message.get("tool_calls")
                if isinstance(tool_calls, list):
                    metrics["tool_call_requests"] += len(tool_calls)
                    if len(tool_calls) >= 4:
                        findings.append(
                            Finding(
                                severity="Medium",
                                title="High tool fan-out in one assistant turn",
                                evidence=[
                                    f"session={session_id}",
                                    f"file={path.name}",
                                    f"tool_calls={len(tool_calls)}",
                                ],
                                recommendation="Cap tool calls per turn and force a planning step before parallel fan-out.",
                            )
                        )

            if role == "tool":
                metrics["tool"] += 1
                error_sig = extract_tool_error_signature(content_text)
                if error_sig:
                    metrics["tool_error_messages"] += 1
                    repeated_error_counter[error_sig] += 1

    for session_id, loop_count in sorted(loop_sessions.items(), key=lambda item: item[1], reverse=True):
        if loop_count >= 2:
            findings.append(
                Finding(
                    severity="High",
                    title="Potential repeated response loop",
                    evidence=[f"session={session_id}", f"repeated_assistant_messages={loop_count}"],
                    recommendation="Add anti-repeat guard in prompt/tool policy and force state update between retries.",
                )
            )

    if metrics["empty_assistant_messages"] > 0:
        findings.append(
            Finding(
                severity="Medium",
                title="Assistant produced empty output",
                evidence=[f"count={metrics['empty_assistant_messages']}"],
                recommendation="Require non-empty assistant fallback message when tool chain produces no final text.",
            )
        )

    if metrics["tool_error_messages"] >= 3:
        findings.append(
            Finding(
                severity="High",
                title="Frequent tool-level failures in trajectories",
                evidence=[
                    f"tool_error_messages={metrics['tool_error_messages']}",
                    f"messages={metrics['messages']}",
                ],
                recommendation="Add preflight checks and structured retry policy for tool calls.",
            )
        )

    for message, count in repeated_error_counter.most_common(3):
        if count >= 2:
            findings.append(
                Finding(
                    severity="Medium",
                    title="Repeated identical tool error",
                    evidence=[f"count={count}", f"snippet={message}"],
                    recommendation="Stop retrying identical failing inputs and branch to fallback handling.",
                )
            )

    return metrics, findings, analyzed_sessions


def analyze_log(log_path: Optional[Path], sessions: Sequence[str], tail: int) -> Tuple[Dict[str, Any], List[Finding]]:
    metrics: Dict[str, Any] = {
        "log_path": str(log_path) if log_path else "",
        "log_lines": 0,
        "log_errors": 0,
        "log_warnings": 0,
    }
    findings: List[Finding] = []

    if not log_path:
        return metrics, findings

    lines = tail_lines(log_path, tail)
    metrics["log_lines"] = len(lines)

    error_messages: Counter[str] = Counter()
    session_hits: Counter[str] = Counter()

    for line in lines:
        if " - ERROR - " in line:
            metrics["log_errors"] += 1
            message = line.split(" - ERROR - ", 1)[-1].strip()
            error_messages[message] += 1
        if " - WARNING - " in line:
            metrics["log_warnings"] += 1

        for session in sessions:
            if session and session != "unknown" and session in line:
                session_hits[session] += 1

    if metrics["log_errors"] >= 5:
        top_message, top_count = ("", 0)
        if error_messages:
            top_message, top_count = error_messages.most_common(1)[0]
        findings.append(
            Finding(
                severity="High",
                title="High error density in recent log tail",
                evidence=[
                    f"log_path={log_path}",
                    f"errors={metrics['log_errors']} / lines={metrics['log_lines']}",
                    f"top_error_count={top_count}",
                    f"top_error={top_message[:180]}",
                ],
                recommendation=infer_hint(top_message),
            )
        )

    for message, count in error_messages.most_common(3):
        if count >= 3:
            findings.append(
                Finding(
                    severity="Medium",
                    title="Repeated runtime error signature in log",
                    evidence=[f"count={count}", f"message={message[:220]}"],
                    recommendation=infer_hint(message),
                )
            )

    if session_hits:
        top_session, hit_count = session_hits.most_common(1)[0]
        findings.append(
            Finding(
                severity="Low",
                title="Session appears frequently in log tail",
                evidence=[f"session={top_session}", f"matched_lines={hit_count}"],
                recommendation="Correlate this session with recent tool errors and inspect its latest trajectory steps.",
            )
        )

    return metrics, findings


def sort_findings(findings: Iterable[Finding]) -> List[Finding]:
    rank = {"High": 0, "Medium": 1, "Low": 2}
    return sorted(findings, key=lambda f: (rank.get(f.severity, 99), f.title))


def render_markdown(
    trajectory_files: Sequence[Path],
    trajectory_metrics: Dict[str, Any],
    log_metrics: Dict[str, Any],
    findings: Sequence[Finding],
) -> str:
    now = datetime.now().isoformat(timespec="seconds")
    lines: List[str] = []
    lines.append("# Trajectory Self-Review Report")
    lines.append("")
    lines.append(f"- Generated at: `{now}`")
    lines.append(f"- Trajectory files analyzed: `{len(trajectory_files)}`")
    lines.append(f"- Log file: `{log_metrics.get('log_path', '') or 'not found'}`")
    lines.append("")
    lines.append("## Scope")
    for path in trajectory_files:
        lines.append(f"- `{path}`")
    lines.append("")
    lines.append("## Metrics")
    lines.append(f"- Messages: `{trajectory_metrics.get('messages', 0)}`")
    lines.append(f"- Assistant messages: `{trajectory_metrics.get('assistant', 0)}`")
    lines.append(f"- Tool messages: `{trajectory_metrics.get('tool', 0)}`")
    lines.append(f"- Tool call requests: `{trajectory_metrics.get('tool_call_requests', 0)}`")
    lines.append(f"- Tool error messages: `{trajectory_metrics.get('tool_error_messages', 0)}`")
    lines.append(f"- Empty assistant messages: `{trajectory_metrics.get('empty_assistant_messages', 0)}`")
    lines.append(f"- Long gaps (>60s): `{trajectory_metrics.get('long_gaps_over_60s', 0)}`")
    lines.append(f"- Log lines analyzed: `{log_metrics.get('log_lines', 0)}`")
    lines.append(f"- Log errors: `{log_metrics.get('log_errors', 0)}`")
    lines.append(f"- Log warnings: `{log_metrics.get('log_warnings', 0)}`")
    lines.append("")
    lines.append("## Findings")

    if not findings:
        lines.append("- No significant issues detected in current scope.")
    else:
        for idx, finding in enumerate(findings, 1):
            lines.append(f"{idx}. **[{finding.severity}] {finding.title}**")
            for item in finding.evidence:
                lines.append(f"   - Evidence: `{item}`")
            lines.append(f"   - Recommendation: {finding.recommendation}")
    lines.append("")
    lines.append("## Next Actions")
    lines.append("1. Fix all `High` findings before changing prompt details.")
    lines.append("2. Re-run this script with the same scope and compare metrics.")
    lines.append("3. If errors persist, capture one failing session and inspect full trace manually.")

    return "\n".join(lines).strip() + "\n"


def main() -> int:
    args = parse_args()

    trajectory_files = discover_trajectory_files(
        pattern=args.trajectory_glob,
        agent=args.agent,
        session=args.session,
        limit=args.limit_files,
    )

    trajectory_metrics, trajectory_findings, sessions = analyze_trajectories(trajectory_files)

    log_path = pick_log_path(args.dolphin_log)
    log_metrics, log_findings = analyze_log(log_path, sessions=sessions, tail=args.tail_lines)

    all_findings = sort_findings([*trajectory_findings, *log_findings])
    report = render_markdown(
        trajectory_files=trajectory_files,
        trajectory_metrics=trajectory_metrics,
        log_metrics=log_metrics,
        findings=all_findings,
    )

    if args.output:
        output_path = Path(args.output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")

    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
