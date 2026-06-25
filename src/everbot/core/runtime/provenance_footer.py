"""#130 T1 — mechanically attach each report signal's top-1 source link at delivery.

Report-producing scripts emit a machine block
``<PROVENANCE>{"signals":[{title,url}, ...]}</PROVENANCE>`` (top-1 per signal) at the end
of stdout. Delivery pulls it from the run's events and renders a footer appended to the
push — independent of the LLM prose, which may drop the links.

Security (#131 review P1): only the block produced by the *trusted producer script*
(``rhino_report.py``) is honored. A response's stdout is trusted only when it is bound (by
``toolCallId``) to a ``run_command`` request whose argv invokes a trusted script. A
``<PROVENANCE>`` tag appearing in any other tool output — e.g. external tweet/web content
flowing through a different command — is ignored, preventing forged source links. URLs are
restricted to http(s); titles are flattened to one line; counts and lengths are capped.
Pure functions, no IO.
"""
from __future__ import annotations

import json
import re
import shlex
from typing import Any, Dict, List

_BLOCK_RE = re.compile(r"<PROVENANCE>(.*?)</PROVENANCE>", re.DOTALL)

# Script-path suffixes whose run_command output may carry a PROVENANCE block. Trust is
# bound to the executed command (which the agent constructs), never to tool output (which
# may contain attacker-influenced external content).
_TRUSTED_PRODUCER_SCRIPTS = ("gray-rhino/scripts/rhino_report.py",)

_MAX_SIGNALS = 20
_MAX_TITLE_LEN = 200
_MAX_URL_LEN = 500
_URL_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


def extract_provenance_block(text: str) -> List[Dict[str, str]]:
    """Parse and validate the PROVENANCE block out of a piece of text.

    Robust + hardened: missing block / bad JSON / bad fields degrade to ``[]`` and never
    raise. Drops signals whose url is not http(s) or is over-long; flattens whitespace in
    titles (no newlines breaking the Markdown list); caps the signal count.
    """
    m = _BLOCK_RE.search(text or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except (ValueError, TypeError):
        return []
    signals = data.get("signals") if isinstance(data, dict) else None
    if not isinstance(signals, list):
        return []
    out: List[Dict[str, str]] = []
    for s in signals:
        if not isinstance(s, dict):
            continue
        title, url = s.get("title"), s.get("url")
        if not (isinstance(title, str) and title and isinstance(url, str) and url):
            continue
        if not _URL_SCHEME_RE.match(url) or len(url) > _MAX_URL_LEN:
            continue
        title = _WS_RE.sub(" ", title).strip()[:_MAX_TITLE_LEN]
        if not title:
            continue
        out.append({"title": title, "url": url})
        if len(out) >= _MAX_SIGNALS:
            break
    return out


def _command_is_trusted(command: Any, suffixes) -> bool:
    """True when the shell command's argv invokes one of the trusted producer scripts."""
    if not isinstance(command, str):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    return any(tok.endswith(suf) for tok in argv for suf in suffixes)


def extract_signals_from_events(
    events: List[Dict[str, Any]],
    trusted_script_suffixes=_TRUSTED_PRODUCER_SCRIPTS,
) -> List[Dict[str, str]]:
    """Extract PROVENANCE signals from a run's events, trusting ONLY the output of a
    trusted producer script's ``run_command`` (bound by ``toolCallId`` to its request).

    A tag in any other tool output is ignored (anti-forgery). Returns the last trusted
    block found. Robust: any missing/mismatched field is skipped; never raises.
    """
    # toolCallIds of run_command requests whose argv invokes a trusted producer script.
    trusted_calls = set()
    for e in events:
        if not isinstance(e, dict) or e.get("type") != "tool.requested":
            continue
        p = e.get("payload") or {}
        if p.get("toolName") != "run_command":
            continue
        call_id = p.get("toolCallId")
        command = (p.get("input") or {}).get("command")
        if call_id is not None and _command_is_trusted(command, trusted_script_suffixes):
            trusted_calls.add(call_id)

    found: List[Dict[str, str]] = []
    for e in events:
        if not isinstance(e, dict) or e.get("type") != "tool.responded":
            continue
        p = e.get("payload") or {}
        if p.get("toolCallId") not in trusted_calls:
            continue
        output = p.get("output")
        stdout = output.get("stdout") if isinstance(output, dict) else None
        if not isinstance(stdout, str):
            continue
        signals = extract_provenance_block(stdout)
        if signals:
            found = signals
    return found


# User-facing footer header (delivered content is Chinese, matching the report).
_FOOTER_HEADER = "📎 原文链接（机械附加，未经 LLM 改写）"


def render_provenance_footer(signals: List[Dict[str, str]]) -> str:
    """Render signals into a footer appended to the report. Empty -> "" (body unchanged)."""
    if not signals:
        return ""
    lines = [f"- {s['title']} — {s['url']}" for s in signals]
    return "\n\n" + _FOOTER_HEADER + "\n" + "\n".join(lines)


def append_provenance_footer(result: str, events: List[Dict[str, Any]]) -> str:
    """Mechanical pre-delivery step: strip any raw ``<PROVENANCE>`` block the LLM may have
    echoed into its prose (never leak the tag to the user), then pull trusted evidence from
    the run events and append a clean footer. No evidence -> just the stripped body.
    Independent of the LLM prose.
    """
    cleaned = _BLOCK_RE.sub("", result or "").rstrip()
    footer = render_provenance_footer(extract_signals_from_events(events))
    return cleaned + footer if footer else cleaned
