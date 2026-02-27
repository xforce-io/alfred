"""Turn orchestrator: shared execution layer for LLM turn lifecycle.

Encapsulates retry, tool budget, failure-signature tracking, and streaming
event normalisation.  Both ChatService (primary/sub) and HeartbeatRunner
(heartbeat/job) consume the same ``run_turn()`` async iterator so that
execution policies are defined once.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Union


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------

class TurnEventType(str, Enum):
    LLM_DELTA = "llm_delta"
    TOOL_CALL = "tool_call"
    TOOL_OUTPUT = "tool_output"
    SKILL = "skill"
    STATUS = "status"
    TURN_COMPLETE = "turn_complete"
    TURN_ERROR = "turn_error"


@dataclass
class TurnEvent:
    """Normalised event emitted by :meth:`TurnOrchestrator.run_turn`."""

    type: TurnEventType
    # Content fields (only relevant subset populated per type)
    content: str = ""
    tool_name: str = ""
    tool_args: str = ""
    tool_output: str = ""
    skill_name: str = ""
    skill_args: str = ""
    skill_output: str = ""
    pid: str = ""
    status: str = ""
    reference_id: str = ""
    error: str = ""
    # Truncation metadata
    args_truncated: bool = False
    args_total_chars: int = 0
    output_truncated: bool = False
    output_total_chars: int = 0
    # Aggregated answer so far (populated on TURN_COMPLETE)
    answer: str = ""
    # Stats (populated on TURN_COMPLETE / TURN_ERROR)
    tool_call_count: int = 0
    tool_execution_count: int = 0
    tool_names_executed: List[str] = field(default_factory=list)
    failed_tool_outputs: int = 0


@dataclass
class TurnPolicy:
    """Configurable knobs consumed by the orchestrator."""

    max_attempts: int = 3
    max_tool_calls: int = 50
    max_failed_tool_outputs: int = 6
    max_same_failure_signature: int = 4
    max_same_tool_intent: int = 3
    max_non_progress_events: int = 500
    max_tool_args_preview_chars: int = 500
    max_tool_output_preview_chars: int = 8000
    timeout_seconds: Optional[float] = None
    retryable_markers: List[str] = field(default_factory=lambda: [
        "incomplete chunked read",
        "peer closed connection",
        "connecterror",
        "connection error",
        "apiconnectionerror",
        "timeout",
        "remote disconnected",
        "connection broken",
        "run_coroutine_failed",
    ])
    # Internal helper tools excluded from tool-call budget counting.
    budget_exempt_tools: frozenset = field(default_factory=lambda: frozenset({
        "_load_resource_skill",
        "_load_skill_resource",
        "_get_cached_result_detail",
    }))


# Convenience presets
CHAT_POLICY = TurnPolicy()

HEARTBEAT_POLICY = TurnPolicy(
    max_attempts=3,
    max_tool_calls=10,
    timeout_seconds=120,
)

JOB_POLICY = TurnPolicy(
    max_attempts=1,
    max_tool_calls=20,
    max_failed_tool_outputs=5,
    max_tool_output_preview_chars=12000,
)


# ---------------------------------------------------------------------------
# Helpers (stateless, extracted from ChatService)
# ---------------------------------------------------------------------------

def _is_retryable(exc: Exception, markers: List[str]) -> bool:
    error_str = str(exc).lower()
    return any(m in error_str for m in markers)


def _extract_failure_signature(output: str) -> Optional[str]:
    if not output:
        return None
    code_match = re.search(r"Command exited with code\s+(\d+)", output)
    if code_match and code_match.group(1) != "0":
        exit_code = code_match.group(1)
        # Try to extract a structured error_code from JSON body for finer
        # granularity (e.g. "PATH_NOT_FOUND" vs "WORKSPACE_LOCKED"), so that
        # distinct errors are not collapsed into the same signature.
        error_code_match = re.search(r'"error_code"\s*:\s*"([^"]+)"', output)
        if error_code_match:
            return f"exit_code:{exit_code}:{error_code_match.group(1)}"
        return f"exit_code:{exit_code}"
    # Match "Error:" only at line start (possibly with leading whitespace or a
    # class-name prefix like "SyntaxError:"), avoiding false positives from
    # legitimate output that happens to contain the word "Error:" mid-sentence.
    error_line_match = re.search(r"(?m)^\s*\w*Error:", output)
    if error_line_match:
        line = output[error_line_match.start():].split("\n", 1)[0].strip()
        return f"error:{line[:120]}" if line else "error"
    for marker in ("ERR_CONNECTION", "ECONNREFUSED", "SSL_ERROR", "Connection refused"):
        if marker in output:
            return marker
    return None


_FILE_PATH_PATTERNS = [
    r'(?:cat\s*>|touch|echo\s*>|tee)\s+([^\s<>|&;]+)',
    r'cat\s*>\s*([^\s<]+)\s*<<',
    r'open\s*\(\s*["\']([^"\']+)["\']',
    r'Path\s*\(\s*["\']([^"\']+)["\']',
    r'(?:mkdir|rm|cp|mv|ls)\s+(?:-\w+\s+)*([^\s|&;]+)',
]


def _extract_tool_intent_signature(tool_name: str, args) -> Optional[str]:
    if not tool_name or not args:
        return None
    if not isinstance(args, str):
        args = str(args)
    args_lower = args.lower()
    for pattern in _FILE_PATH_PATTERNS:
        match = re.search(pattern, args, re.IGNORECASE)
        if match:
            file_path = match.group(1).strip("\"'")
            if tool_name == "_bash":
                if any(op in args_lower for op in ['cat >', 'cat>', 'echo >', 'tee', 'heredoc', '<<']):
                    return f"write_file:{file_path}"
                elif 'mkdir' in args_lower:
                    return f"create_dir:{file_path}"
                elif 'touch' in args_lower:
                    return f"create_file:{file_path}"
                elif 'rm ' in args_lower:
                    return f"delete:{file_path}"
            elif tool_name == "_python":
                if 'open' in args_lower and ("'w'" in args_lower or '"w"' in args_lower):
                    return f"write_file:{file_path}"
                elif '.write' in args_lower:
                    return f"write_file:{file_path}"
    if tool_name == "_read_file":
        return f"read_file:{args.strip().strip(chr(34) + chr(39))}"
    if tool_name == "_grep":
        try:
            parsed = json.loads(args)
            pattern = parsed.get("pattern", "")
            if pattern:
                return f"search_grep:{pattern}"
        except (json.JSONDecodeError, AttributeError):
            pass
    if tool_name == "_bash":
        grep_match = re.search(r'(?:grep|rg)\s+(?:-\w+\s+)*["\']?([^"\'|\s]+)', args)
        if grep_match:
            return f"search_bash:{grep_match.group(1)}"
    return None


def _truncate_preview(text: str, max_chars: int) -> tuple[str, bool, int]:
    if text is None:
        return "", False, 0
    raw = str(text)
    total = len(raw)
    if total <= max_chars:
        return raw, False, total
    if max_chars < 100:
        omitted = total - max_chars
        return raw[:max_chars] + f"... [truncated {omitted} chars]", True, total
    head = int(max_chars * 0.6)
    tail = max_chars - head - 50
    omitted = total - head - tail
    return (raw[:head] + f"\n\n... [truncated {omitted} chars] ...\n\n" + raw[-tail:]), True, total


# ---------------------------------------------------------------------------
# TurnOrchestrator
# ---------------------------------------------------------------------------

class TurnOrchestrator:
    """Shared execution layer: retry + budget + streaming event normalisation.

    Consumers iterate over ``run_turn()`` and handle each :class:`TurnEvent`
    according to their transport (WebSocket push, collect-and-summarise, …).
    """

    def __init__(self, policy: Optional[TurnPolicy] = None):
        self.policy = policy or TurnPolicy()

    # -- public entry point -------------------------------------------------

    async def run_turn(
        self,
        agent: Any,
        message: Union[str, list],
        *,
        system_prompt: str = "",
        stream_mode: str = "delta",
        is_first_turn: bool = False,
        cancel_event: Optional[asyncio.Event] = None,
        on_before_retry: Optional[Callable[[int, Exception], Any]] = None,
    ) -> AsyncIterator[TurnEvent]:
        """Execute one LLM turn and yield normalised :class:`TurnEvent` s.

        Parameters
        ----------
        agent:
            A Dolphin-compatible agent (must expose ``continue_chat``
            and optionally ``arun``).
        message:
            The user / trigger message for this turn.
        system_prompt:
            System prompt override passed to ``continue_chat``.
        is_first_turn:
            If *True* and agent has ``arun``, use ``arun()`` instead.
        cancel_event:
            External cancellation signal (e.g. user interrupt).
        on_before_retry:
            Callback ``(attempt, exception) -> Awaitable|None`` invoked
            before each retry so callers can reset agent state or send
            status updates.
        """
        policy = self.policy

        for attempt in range(max(policy.max_attempts, 1)):
            try:
                async for event in self._run_attempt(
                    agent,
                    message,
                    system_prompt=system_prompt,
                    stream_mode=stream_mode,
                    is_first_turn=is_first_turn,
                    cancel_event=cancel_event,
                ):
                    yield event
                    if event.type == TurnEventType.TURN_ERROR:
                        return  # budget/guard error already emitted
                return  # success
            except Exception as exc:
                is_last = (attempt >= policy.max_attempts - 1)
                if _is_retryable(exc, policy.retryable_markers) and not is_last:
                    if on_before_retry is not None:
                        res = on_before_retry(attempt, exc)
                        if asyncio.iscoroutine(res):
                            await res
                    yield TurnEvent(
                        type=TurnEventType.STATUS,
                        content=f"Transient error, retrying ({attempt + 1}/{policy.max_attempts})…",
                        error=str(exc),
                    )
                    await asyncio.sleep((attempt + 1) * 1.5)
                    continue
                # Non-retryable or final attempt
                yield TurnEvent(type=TurnEventType.TURN_ERROR, error=str(exc))
                return

    # -- single attempt -----------------------------------------------------

    async def _run_attempt(
        self,
        agent: Any,
        message: Union[str, list],
        *,
        system_prompt: str,
        stream_mode: str,
        is_first_turn: bool,
        cancel_event: Optional[asyncio.Event],
    ) -> AsyncIterator[TurnEvent]:
        policy = self.policy

        # Choose entry point.
        # Always use continue_chat when a user message is present so it is
        # never silently discarded.  arun (autonomous mode) is reserved for
        # daemon-initiated turns where there is no user message.
        if is_first_turn and hasattr(agent, "arun") and not message:
            event_stream = agent.arun(run_mode=True, stream_mode=stream_mode, mode="tool_call")
        else:
            event_stream = agent.continue_chat(
                message=message, stream_mode=stream_mode, mode="tool_call", system_prompt=system_prompt,
            )

        # Optionally wrap with timeout
        if policy.timeout_seconds:
            event_stream = _timeout_wrapper(event_stream, policy.timeout_seconds)

        # Tracking state
        response = ""
        tool_call_count = 0
        tool_execution_count = 0
        tool_names_executed: list[str] = []
        failed_tool_outputs = 0
        failure_signatures: Dict[str, int] = {}
        tool_intent_signatures: Dict[str, int] = {}
        sent_progress: Dict[str, str] = {}
        non_progress_count = 0
        llm_started = False

        async for event in event_stream:
            # External cancellation
            if cancel_event is not None and cancel_event.is_set():
                yield TurnEvent(type=TurnEventType.TURN_ERROR, error="cancelled")
                return

            if not isinstance(event, dict) or "_progress" not in event:
                non_progress_count += 1
                if non_progress_count >= policy.max_non_progress_events:
                    yield TurnEvent(
                        type=TurnEventType.TURN_ERROR,
                        error="TOO_MANY_NON_PROGRESS_EVENTS",
                    )
                    return
                continue

            non_progress_count = 0
            for progress in event.get("_progress", []):
                pid = progress.get("id") or ""
                status = progress.get("status") or ""
                stage = progress.get("stage")

                if stage == "llm":
                    delta = progress.get("delta", "")
                    answer = progress.get("answer", "")
                    if delta:
                        if not llm_started:
                            llm_started = True
                        response += delta
                        yield TurnEvent(type=TurnEventType.LLM_DELTA, content=delta)
                    if answer and not response:
                        response = answer
                        if not llm_started:
                            llm_started = True

                elif stage == "skill":
                    if pid and sent_progress.get(pid) == status:
                        continue
                    skill_info = progress.get("skill_info") or {}
                    s_name = skill_info.get("name") or progress.get("tool_name") or ""
                    s_args = skill_info.get("args") or progress.get("args") or ""
                    s_output = progress.get("answer") or progress.get("block_answer") or progress.get("output") or ""

                    fail_sig = None  # set in completed/failed branch below

                    if status in ("running", "processing"):
                        # Skill invocation → count as tool call (unless exempt)
                        if s_name not in policy.budget_exempt_tools:
                            tool_call_count += 1
                        if tool_call_count > policy.max_tool_calls:
                            yield TurnEvent(
                                type=TurnEventType.TURN_ERROR,
                                error=f"TOOL_CALL_BUDGET_EXCEEDED: tool_calls={tool_call_count}, limit={policy.max_tool_calls}",
                                answer=response,
                                tool_call_count=tool_call_count,
                                tool_execution_count=tool_execution_count,
                                tool_names_executed=list(tool_names_executed),
                                failed_tool_outputs=failed_tool_outputs,
                            )
                            return

                        # Intent dedup check
                        intent_sig = _extract_tool_intent_signature(s_name, s_args)
                        if intent_sig:
                            tool_intent_signatures[intent_sig] = tool_intent_signatures.get(intent_sig, 0) + 1
                            if tool_intent_signatures[intent_sig] > policy.max_same_tool_intent:
                                yield TurnEvent(
                                    type=TurnEventType.TURN_ERROR,
                                    error=(
                                        f"REPEATED_TOOL_INTENT: intent={intent_sig}, "
                                        f"count={tool_intent_signatures[intent_sig]}, limit={policy.max_same_tool_intent}"
                                    ),
                                    answer=response,
                                    tool_call_count=tool_call_count,
                                    tool_execution_count=tool_execution_count,
                                    tool_names_executed=list(tool_names_executed),
                                    failed_tool_outputs=failed_tool_outputs,
                                )
                                return

                        tool_execution_count += 1
                        tool_names_executed.append(s_name)

                    elif status in ("completed", "failed"):
                        # Skill result → track failures
                        fail_sig = _extract_failure_signature(s_output)
                        if fail_sig:
                            failed_tool_outputs += 1
                            failure_signatures[fail_sig] = failure_signatures.get(fail_sig, 0) + 1
                            if (
                                failed_tool_outputs >= policy.max_failed_tool_outputs
                                or failure_signatures[fail_sig] >= policy.max_same_failure_signature
                            ):
                                yield TurnEvent(
                                    type=TurnEventType.TURN_ERROR,
                                    error=(
                                        f"REPEATED_TOOL_FAILURES: failed={failed_tool_outputs}, "
                                        f"signature={fail_sig}, count={failure_signatures[fail_sig]}"
                                    ),
                                    answer=response,
                                    tool_call_count=tool_call_count,
                                    tool_execution_count=tool_execution_count,
                                    tool_names_executed=list(tool_names_executed),
                                    failed_tool_outputs=failed_tool_outputs,
                                )
                                return

                    # Inject failure count warning for skill outputs
                    warn_output = s_output
                    if fail_sig and failed_tool_outputs >= 1:
                        sig_count = failure_signatures.get(fail_sig, 0)
                        sig_max = policy.max_same_failure_signature
                        total_max = policy.max_failed_tool_outputs
                        warn_output = s_output + (
                            f"\n[⚠ tool_failure {failed_tool_outputs}/{total_max}"
                            f" (sig {sig_count}/{sig_max}): {fail_sig}."
                            f" Switch strategy to avoid circuit break.]"
                        )

                    yield TurnEvent(
                        type=TurnEventType.SKILL,
                        pid=pid, status=status,
                        skill_name=s_name, skill_args=s_args, skill_output=warn_output,
                    )
                    if pid:
                        sent_progress[pid] = status

                elif stage == "tool_call":
                    t_name = progress.get("tool_name", "")
                    if t_name not in policy.budget_exempt_tools:
                        tool_call_count += 1
                    if tool_call_count > policy.max_tool_calls:
                        yield TurnEvent(
                            type=TurnEventType.TURN_ERROR,
                            error=f"TOOL_CALL_BUDGET_EXCEEDED: tool_calls={tool_call_count}, limit={policy.max_tool_calls}",
                            answer=response,
                            tool_call_count=tool_call_count,
                            tool_execution_count=tool_execution_count,
                            tool_names_executed=list(tool_names_executed),
                            failed_tool_outputs=failed_tool_outputs,
                        )
                        return
                    if pid and sent_progress.get(pid) == status:
                        continue
                    t_args_raw = progress.get("args", "")

                    # Intent dedup check
                    intent_sig = _extract_tool_intent_signature(t_name, t_args_raw)
                    if intent_sig:
                        tool_intent_signatures[intent_sig] = tool_intent_signatures.get(intent_sig, 0) + 1
                        if tool_intent_signatures[intent_sig] > policy.max_same_tool_intent:
                            yield TurnEvent(
                                type=TurnEventType.TURN_ERROR,
                                error=(
                                    f"REPEATED_TOOL_INTENT: intent={intent_sig}, "
                                    f"count={tool_intent_signatures[intent_sig]}, limit={policy.max_same_tool_intent}"
                                ),
                                answer=response,
                                tool_call_count=tool_call_count,
                                tool_execution_count=tool_execution_count,
                                tool_names_executed=list(tool_names_executed),
                                failed_tool_outputs=failed_tool_outputs,
                            )
                            return

                    args_preview, args_trunc, args_total = _truncate_preview(t_args_raw, policy.max_tool_args_preview_chars)
                    tool_execution_count += 1
                    tool_names_executed.append(t_name)
                    yield TurnEvent(
                        type=TurnEventType.TOOL_CALL,
                        pid=pid, status=status,
                        tool_name=t_name, tool_args=args_preview,
                        args_truncated=args_trunc, args_total_chars=args_total,
                    )
                    if pid:
                        sent_progress[pid] = status

                elif stage == "tool_output":
                    if pid and sent_progress.get(pid) == status:
                        continue
                    t_output_raw = progress.get("output", "")
                    fail_sig = _extract_failure_signature(t_output_raw)
                    if fail_sig:
                        failed_tool_outputs += 1
                        failure_signatures[fail_sig] = failure_signatures.get(fail_sig, 0) + 1
                        if (
                            failed_tool_outputs >= policy.max_failed_tool_outputs
                            or failure_signatures[fail_sig] >= policy.max_same_failure_signature
                        ):
                            yield TurnEvent(
                                type=TurnEventType.TURN_ERROR,
                                error=(
                                    f"REPEATED_TOOL_FAILURES: failed={failed_tool_outputs}, "
                                    f"signature={fail_sig}, count={failure_signatures[fail_sig]}"
                                ),
                                answer=response,
                                tool_call_count=tool_call_count,
                                tool_execution_count=tool_execution_count,
                                tool_names_executed=list(tool_names_executed),
                                failed_tool_outputs=failed_tool_outputs,
                            )
                            return

                    out_preview, out_trunc, out_total = _truncate_preview(t_output_raw, policy.max_tool_output_preview_chars)

                    # Inject failure count warning so LLM can see how close
                    # it is to the circuit breaker and switch strategy.
                    if fail_sig and failed_tool_outputs >= 1:
                        sig_count = failure_signatures.get(fail_sig, 0)
                        sig_max = policy.max_same_failure_signature
                        total_max = policy.max_failed_tool_outputs
                        out_preview += (
                            f"\n[⚠ tool_failure {failed_tool_outputs}/{total_max}"
                            f" (sig {sig_count}/{sig_max}): {fail_sig}."
                            f" Switch strategy to avoid circuit break.]"
                        )

                    yield TurnEvent(
                        type=TurnEventType.TOOL_OUTPUT,
                        pid=pid, status="failed" if fail_sig else "success",
                        tool_name=progress.get("tool_name", ""),
                        tool_output=out_preview,
                        output_truncated=out_trunc, output_total_chars=out_total,
                        reference_id=progress.get("reference_id", ""),
                    )
                    if pid:
                        sent_progress[pid] = status

        # Stream exhausted without error → success
        yield TurnEvent(
            type=TurnEventType.TURN_COMPLETE,
            answer=response,
            tool_call_count=tool_call_count,
            tool_execution_count=tool_execution_count,
            tool_names_executed=list(tool_names_executed),
            failed_tool_outputs=failed_tool_outputs,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _timeout_wrapper(stream: AsyncIterator, timeout: float) -> AsyncIterator:
    """Wrap an async iterator with a total timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    async for item in stream:
        if asyncio.get_event_loop().time() > deadline:
            raise asyncio.TimeoutError(f"Turn exceeded {timeout}s timeout")
        yield item
