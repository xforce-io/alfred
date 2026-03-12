"""Turn orchestrator: shared execution layer for LLM turn lifecycle.

Encapsulates retry, tool budget, failure-signature tracking, and streaming
event normalisation.  Both ChatService (primary/sub) and HeartbeatRunner
(heartbeat/job) consume the same ``run_turn()`` async iterator so that
execution policies are defined once.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Union

# Public data types and policy factories — extracted to turn_policy.py
from .turn_policy import (  # noqa: F401 — re-exported
    TurnEventType,
    TurnEvent,
    TurnPolicy,
    CHAT_POLICY,
    HEARTBEAT_POLICY,
    JOB_POLICY,
    WORKFLOW_POLICY,
    _POLICY_DEFAULTS,
    _resolve_timeout,
    build_chat_policy,
    build_heartbeat_policy,
    build_job_policy,
    build_workflow_policy,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers (stateless, extracted from ChatService)
# ---------------------------------------------------------------------------

def _is_retryable(exc: Exception, markers: List[str]) -> bool:
    # Turn-level timeouts are NOT transient network errors — retrying would
    # repeat the same expensive work that already timed out.
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return False
    error_str = str(exc).lower()
    return any(m in error_str for m in markers)


def _extract_failure_signature(output: str) -> Optional[str]:
    if not output:
        return None
    code_match = re.search(r"(?m)^\s*Command exited with code\s+(\d+)\b", output)
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
            path = parsed.get("path", "")
            # Flag searches targeting .venv or site-packages directories
            if path and any(
                excl in path
                for excl in (".venv", "site-packages", "node_modules")
            ):
                return f"search_grep_excluded:{pattern}:{path}"
            if pattern:
                return f"search_grep:{pattern}"
        except (json.JSONDecodeError, AttributeError):
            pass
    if tool_name == "_bash":
        # Detect command_id continuation/wait pattern — repeated waits on the
        # same long-running command should be capped like any other intent.
        cid_match = re.search(r'command_id["\'\s]*[=:]\s*["\'\s]*([a-f0-9]{6,})', args)
        if cid_match:
            return f"bash_wait:{cid_match.group(1)}"
        # Detect web-search script calls — group all search queries under a
        # single intent so that repeated searches with different keywords are
        # caught by the intent-dedup guard.
        if re.search(r'(?:web-search|web_search)[/\\].*?search\.py\b', args):
            return "web_search"
        grep_match = re.search(r'(?:grep|rg)\s+(?:-\w+\s+)*["\']?([^"\'|\s]+)', args)
        if grep_match:
            return f"search_bash:{grep_match.group(1)}"
        normalized = re.sub(r"\s+", " ", args.strip())
        if normalized:
            cmd_hash = hashlib.sha256(normalized.encode()).hexdigest()[:12]
            # Classify common read-only bash commands so they get the
            # higher max_same_readonly_intent limit instead of the
            # stricter write-intent limit.
            if _is_read_only_bash(args_lower):
                return f"bash_read:{cmd_hash}"
            return f"bash_exec:{cmd_hash}"
    # Generic fallback for _python calls: hash the normalized code so that
    # identical calls (e.g. repeated whisper.transcribe()) are caught by the
    # REPEATED_TOOL_INTENT guard even when they don't match file-path patterns.
    if tool_name == "_python":
        normalized = re.sub(r'\s+', ' ', args.strip())
        code_hash = hashlib.sha256(normalized.encode()).hexdigest()[:12]
        return f"python_exec:{code_hash}"
    return None


_READ_ONLY_BASH_PATTERNS = re.compile(
    r"""(?:^|&&|\|\||;)\s*(?:
        git\s+(?:status|diff|log|show|branch|tag|remote|stash\s+list|rev-parse|describe)
        |ls(?:\s|$)
        |cat\s
        |head\s
        |tail\s
        |wc\s
        |file\s
        |stat\s
        |du\s
        |df\s
        |pwd
        |which\s
        |type\s
        |echo\s
        |python[3]?\s+(?:-c\s|.*\bprint\b)
    )""",
    re.VERBOSE | re.IGNORECASE,
)

# Patterns that indicate write operations — these override read-only detection.
_WRITE_BASH_MARKERS = (
    "cat >", "cat>", "echo >", "echo>", "tee ", ">>",
    "sed -i", "mv ", "cp ", "rm ", "mkdir ", "touch ",
    "chmod ", "chown ", "git add", "git commit", "git push",
    "git checkout", "git reset", "git merge", "git rebase",
    "pip install", "npm install", "apt ", "brew ",
)


def _is_read_only_bash(args_lower: str) -> bool:
    """Heuristic: return True if a bash command looks read-only."""
    if any(m in args_lower for m in _WRITE_BASH_MARKERS):
        return False
    return bool(_READ_ONLY_BASH_PATTERNS.search(args_lower))


_READ_ONLY_INTENT_PREFIXES = frozenset({
    "read_file:", "search_grep:", "search_bash:", "bash_read:",
})


def _is_read_only_intent(intent_sig: str) -> bool:
    """Return True if the intent signature represents a read-only operation."""
    return any(intent_sig.startswith(p) for p in _READ_ONLY_INTENT_PREFIXES)


_FINGERPRINT_CHARS = 120  # chars of LLM text to fingerprint per round


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

    def __init__(
        self,
        policy: Optional[TurnPolicy] = None,
        prior_failures: Optional[Dict[str, int]] = None,
    ):
        self.policy = policy or TurnPolicy()
        # Cross-turn failure memory: maps failure signatures to counts
        # accumulated from previous turns.  Callers can pass the
        # ``accumulated_failures`` dict back on the next turn to carry
        # over context.
        self._prior_failures: Dict[str, int] = dict(prior_failures or {})
        # After run_turn completes, callers can read this to persist
        # accumulated failure counts for the next turn.
        self.accumulated_failures: Dict[str, int] = dict(self._prior_failures)

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
        on_deferred_result: Optional[Callable] = None,
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
                    on_deferred_result=on_deferred_result,
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

    # -- guard helpers ------------------------------------------------------

    def _check_empty_output_loop(
        self,
        llm_had_output_this_round: bool,
        tool_execution_count: int,
        consecutive_empty_llm_rounds: int,
        response: str,
        tool_call_count: int,
        tool_names_executed: list,
        failed_tool_outputs: int,
    ) -> Optional[TurnEvent]:
        """Return a TURN_ERROR event if too many consecutive tool calls had no LLM output."""
        if llm_had_output_this_round or tool_execution_count == 0:
            return None
        consecutive_empty_llm_rounds += 1
        if consecutive_empty_llm_rounds >= self.policy.max_consecutive_empty_llm_rounds:
            return TurnEvent(
                type=TurnEventType.TURN_ERROR,
                error=(
                    f"EMPTY_OUTPUT_LOOP: {consecutive_empty_llm_rounds} consecutive "
                    f"tool calls with no LLM text output (model likely degraded)"
                ),
                answer=response,
                tool_call_count=tool_call_count,
                tool_execution_count=tool_execution_count,
                tool_names_executed=list(tool_names_executed),
                failed_tool_outputs=failed_tool_outputs,
            )
        return None

    def _check_intent_dedup(
        self,
        intent_sig: Optional[str],
        tool_intent_signatures: Dict[str, int],
        warned_intents: set,
        response: str,
        tool_call_count: int,
        tool_execution_count: int,
        tool_names_executed: list,
        failed_tool_outputs: int,
    ) -> Optional[TurnEvent]:
        """Check for repeated tool intent.

        Returns a TURN_ERROR event when hard limit is exceeded, or *None*.
        When ``count == limit`` the intent is added to *warned_intents* so the
        caller can inject a warning into the tool output, giving the LLM one
        last chance to self-correct before the next call triggers a hard stop.
        """
        if not intent_sig:
            return None
        tool_intent_signatures[intent_sig] = tool_intent_signatures.get(intent_sig, 0) + 1
        limit = (
            self.policy.max_same_readonly_intent
            if _is_read_only_intent(intent_sig)
            else self.policy.max_same_tool_intent
        )
        count = tool_intent_signatures[intent_sig]
        if count > limit:
            return TurnEvent(
                type=TurnEventType.TURN_ERROR,
                error=(
                    f"REPEATED_TOOL_INTENT: intent={intent_sig}, "
                    f"count={count}, limit={limit}"
                ),
                answer=response,
                tool_call_count=tool_call_count,
                tool_execution_count=tool_execution_count,
                tool_names_executed=list(tool_names_executed),
                failed_tool_outputs=failed_tool_outputs,
            )
        if count == limit:
            warned_intents.add(intent_sig)
        return None

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
        on_deferred_result: Optional[Callable] = None,
    ) -> AsyncIterator[TurnEvent]:
        policy = self.policy
        # Apply repeated_failure_limit alias if set
        effective_same_failure_limit = (
            policy.repeated_failure_limit
            if policy.repeated_failure_limit is not None
            else policy.max_same_failure_signature
        )

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
            event_stream = _timeout_wrapper(
                event_stream,
                policy.timeout_seconds,
                on_timeout_drain=on_deferred_result,
                drain_extra_seconds=policy.drain_extra_seconds or 300,
            )

        # Tracking state — pre-seed from prior turns so cross-turn
        # repeated failures are caught early.
        response = ""
        tool_call_count = 0
        tool_execution_count = 0
        tool_names_executed: list[str] = []
        failed_tool_outputs = sum(self._prior_failures.values())
        failure_signatures: Dict[str, int] = dict(self._prior_failures)
        tool_intent_signatures: Dict[str, int] = {}
        warned_intents: set = set()
        pid_to_intent: Dict[str, str] = {}
        sent_progress: Dict[str, str] = {}
        non_progress_count = 0
        llm_started = False
        # Empty-output loop detection: track whether LLM produced any text
        # delta between consecutive tool invocations.  If the LLM triggers
        # tool calls N times in a row without emitting any visible output,
        # the model has likely degraded (e.g. high-context collapse) and we
        # should stop early rather than burning tokens in a loop.
        llm_had_output_this_round = False
        consecutive_empty_llm_rounds = 0
        last_successful_tool_output = ""  # fallback when LLM returns empty
        output_chars = 0  # approximate output token tracking (chars produced by LLM)
        # Repeated-text loop detection: track LLM text fingerprint per round.
        _round_text = ""
        _prev_fp = ""
        _similar_rounds = 0

        def _check_round_text_loop() -> bool:
            """Compare current round's text to previous; return True if limit hit."""
            nonlocal _round_text, _prev_fp, _similar_rounds
            text = _round_text
            _round_text = ""
            fp = hashlib.sha256(text.strip()[:_FINGERPRINT_CHARS].encode()).hexdigest()[:12] if text else ""
            if fp and _prev_fp:
                _similar_rounds = _similar_rounds + 1 if fp == _prev_fp else 0
            _prev_fp = fp
            return _similar_rounds >= policy.max_consecutive_similar_llm_rounds

        async for event in event_stream:
            # External cancellation — emit partial results instead of
            # discarding all work done so far.
            if cancel_event is not None and cancel_event.is_set():
                estimated_tokens = max(1, output_chars // 4) if output_chars > 0 else 0
                yield TurnEvent(
                    type=TurnEventType.TURN_COMPLETE,
                    answer=response or last_successful_tool_output,
                    tool_call_count=tool_call_count,
                    tool_execution_count=tool_execution_count,
                    tool_names_executed=list(tool_names_executed),
                    failed_tool_outputs=failed_tool_outputs,
                    output_tokens=estimated_tokens,
                    status="cancelled",
                )
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
                    think = progress.get("think", "")
                    output_chars += len(delta) + len(think)
                    if delta:
                        if not llm_started:
                            llm_started = True
                        llm_had_output_this_round = True
                        consecutive_empty_llm_rounds = 0
                        response += delta
                        _round_text += delta
                        yield TurnEvent(type=TurnEventType.LLM_DELTA, content=delta)
                    if answer and not response:
                        response = answer
                        if not llm_started:
                            llm_started = True
                        llm_had_output_this_round = True
                        consecutive_empty_llm_rounds = 0
                    # Reasoning output (think) indicates the model is actively
                    # working, even when it produces no user-visible text
                    # (e.g. deciding to call a tool).  Mark the round as
                    # having output so the first few think-only rounds don't
                    # trigger a false positive, but do NOT reset the
                    # consecutive counter — if the model keeps calling tools
                    # with think-only output (no visible delta), it's likely
                    # stuck in a loop (e.g. repeated whisper calls).
                    if think and not llm_had_output_this_round:
                        llm_had_output_this_round = True

                elif stage == "skill":
                    if pid and sent_progress.get(pid) == status:
                        continue
                    skill_info = progress.get("skill_info") or {}
                    s_name = skill_info.get("name") or progress.get("tool_name") or ""
                    s_args = skill_info.get("args") or progress.get("args") or ""
                    s_output = progress.get("answer") or progress.get("block_answer") or progress.get("output") or ""

                    fail_sig = None  # set in completed/failed branch below

                    if status in ("running", "processing"):
                        # Empty-output loop detection
                        err = self._check_empty_output_loop(
                            llm_had_output_this_round, tool_execution_count,
                            consecutive_empty_llm_rounds, response,
                            tool_call_count, tool_names_executed, failed_tool_outputs,
                        )
                        if err:
                            yield err
                            return
                        # Repeated-text loop detection
                        if tool_execution_count > 0 and _check_round_text_loop():
                            yield TurnEvent(
                                type=TurnEventType.TURN_ERROR,
                                error=f"REPEATED_TEXT_LOOP: {_similar_rounds + 1} consecutive similar LLM outputs",
                                answer=response, tool_call_count=tool_call_count,
                                tool_execution_count=tool_execution_count,
                                tool_names_executed=list(tool_names_executed),
                                failed_tool_outputs=failed_tool_outputs,
                            )
                            return
                        if not llm_had_output_this_round and tool_execution_count > 0:
                            consecutive_empty_llm_rounds += 1
                        llm_had_output_this_round = False

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
                        err = self._check_intent_dedup(
                            intent_sig, tool_intent_signatures, warned_intents,
                            response, tool_call_count, tool_execution_count,
                            tool_names_executed, failed_tool_outputs,
                        )
                        if err:
                            yield err
                            return
                        if pid and intent_sig:
                            pid_to_intent[pid] = intent_sig

                        tool_execution_count += 1
                        tool_names_executed.append(s_name)

                    elif status in ("completed", "failed"):
                        # Skill result → track failures
                        fail_sig = _extract_failure_signature(s_output)
                        if fail_sig:
                            failed_tool_outputs += 1
                            failure_signatures[fail_sig] = failure_signatures.get(fail_sig, 0) + 1
                            self.accumulated_failures[fail_sig] = failure_signatures[fail_sig]
                            if (
                                failed_tool_outputs >= policy.max_failed_tool_outputs
                                or failure_signatures[fail_sig] >= effective_same_failure_limit
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

                    # Track last successful skill output for fallback
                    # Check both: no failure signature AND status is not explicitly "failed"
                    if not fail_sig and s_output and status != "failed":
                        last_successful_tool_output = s_output[:policy.max_tool_output_preview_chars]

                    # Inject failure count warning for skill outputs
                    warn_output = s_output
                    if fail_sig and failed_tool_outputs >= 1:
                        sig_count = failure_signatures.get(fail_sig, 0)
                        sig_max = effective_same_failure_limit
                        total_max = policy.max_failed_tool_outputs
                        warn_output = s_output + (
                            f"\n[⚠ tool_failure {failed_tool_outputs}/{total_max}"
                            f" (sig {sig_count}/{sig_max}): {fail_sig}."
                            f" Switch strategy to avoid circuit break.]"
                        )
                    # Inject repeated-intent warning so LLM can self-correct
                    _pid_intent = pid_to_intent.get(pid)
                    if _pid_intent and _pid_intent in warned_intents:
                        warn_output += (
                            f"\n[⚠ repeated_intent: You have already run this"
                            f" same command {tool_intent_signatures.get(_pid_intent, 0)} times."
                            f" Do NOT call it again. Respond to the user based"
                            f" on information you already have.]"
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

                    # Empty-output loop detection
                    err = self._check_empty_output_loop(
                        llm_had_output_this_round, tool_execution_count,
                        consecutive_empty_llm_rounds, response,
                        tool_call_count, tool_names_executed, failed_tool_outputs,
                    )
                    if err:
                        yield err
                        return
                    # Repeated-text loop detection
                    if tool_execution_count > 0 and _check_round_text_loop():
                        yield TurnEvent(
                            type=TurnEventType.TURN_ERROR,
                            error=f"REPEATED_TEXT_LOOP: {_similar_rounds + 1} consecutive similar LLM outputs",
                            answer=response, tool_call_count=tool_call_count,
                            tool_execution_count=tool_execution_count,
                            tool_names_executed=list(tool_names_executed),
                            failed_tool_outputs=failed_tool_outputs,
                        )
                        return
                    if not llm_had_output_this_round and tool_execution_count > 0:
                        consecutive_empty_llm_rounds += 1
                    llm_had_output_this_round = False

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
                    err = self._check_intent_dedup(
                        intent_sig, tool_intent_signatures, warned_intents,
                        response, tool_call_count, tool_execution_count,
                        tool_names_executed, failed_tool_outputs,
                    )
                    if err:
                        yield err
                        return
                    if pid and intent_sig:
                        pid_to_intent[pid] = intent_sig

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
                        self.accumulated_failures[fail_sig] = failure_signatures[fail_sig]
                        if (
                            failed_tool_outputs >= policy.max_failed_tool_outputs
                            or failure_signatures[fail_sig] >= effective_same_failure_limit
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
                        sig_max = effective_same_failure_limit
                        total_max = policy.max_failed_tool_outputs
                        out_preview += (
                            f"\n[⚠ tool_failure {failed_tool_outputs}/{total_max}"
                            f" (sig {sig_count}/{sig_max}): {fail_sig}."
                            f" Switch strategy to avoid circuit break.]"
                        )
                    # Inject repeated-intent warning so LLM can self-correct
                    _pid_intent = pid_to_intent.get(pid)
                    if _pid_intent and _pid_intent in warned_intents:
                        out_preview += (
                            f"\n[⚠ repeated_intent: You have already run this"
                            f" same command {tool_intent_signatures.get(_pid_intent, 0)} times."
                            f" Do NOT call it again. Respond to the user based"
                            f" on information you already have.]"
                        )

                    yield TurnEvent(
                        type=TurnEventType.TOOL_OUTPUT,
                        pid=pid, status="failed" if fail_sig else "success",
                        tool_name=progress.get("tool_name", ""),
                        tool_output=out_preview,
                        output_truncated=out_trunc, output_total_chars=out_total,
                        reference_id=progress.get("reference_id", ""),
                    )
                    if not fail_sig and t_output_raw:
                        last_successful_tool_output = t_output_raw[:policy.max_tool_output_preview_chars]
                    if pid:
                        sent_progress[pid] = status

        # Fallback: if LLM produced no text but the last tool returned
        # substantial output, use that output as the response so the user
        # doesn't get an empty "(无响应)".
        if not response and last_successful_tool_output:
            response = last_successful_tool_output

        # Phantom tool call: model wrote ```bash/python blocks but never
        # issued a real tool_use call — common with weaker models under
        # long context.  Append a short note so the user knows.
        if (
            tool_call_count == 0
            and response
            and re.search(r"```(?:bash|sh|shell|python)\s*\n", response)
        ):
            response += (
                "\n\n---\n⚠️ *[系统] 以上命令未实际执行，"
                "模型仅输出了文本。请复制命令手动运行，或重新描述需求重试。*"
            )

        # Stream exhausted without error → success
        # Estimate output tokens from total chars produced by LLM.
        # This is an approximation (≈ chars / 4 for English, fewer for CJK).
        estimated_tokens = max(1, output_chars // 4) if output_chars > 0 else 0
        yield TurnEvent(
            type=TurnEventType.TURN_COMPLETE,
            answer=response,
            tool_call_count=tool_call_count,
            tool_execution_count=tool_execution_count,
            tool_names_executed=list(tool_names_executed),
            failed_tool_outputs=failed_tool_outputs,
            output_tokens=estimated_tokens,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _timeout_wrapper(
    stream: AsyncIterator,
    timeout: float,
    *,
    on_timeout_drain: Optional[Callable] = None,
    drain_extra_seconds: float = 300,
) -> AsyncIterator:
    """Wrap an async iterator with a total timeout.

    Uses manual ``__anext__()`` instead of ``async for`` so that when a
    timeout occurs the underlying iterator is **not** closed via
    ``athrow()``—it can be handed off to a background drain task.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    aiter = stream.__aiter__()
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            # Already past deadline
            if on_timeout_drain is not None:
                asyncio.create_task(
                    _drain_after_timeout(aiter, on_timeout_drain, drain_extra_seconds),
                    name="deferred-drain",
                )
            else:
                try:
                    await aiter.aclose()
                except Exception:
                    pass
            raise asyncio.TimeoutError(f"Turn exceeded {timeout}s timeout")
        try:
            item = await asyncio.wait_for(aiter.__anext__(), timeout=remaining)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            if on_timeout_drain is not None:
                asyncio.create_task(
                    _drain_after_timeout(aiter, on_timeout_drain, drain_extra_seconds),
                    name="deferred-drain",
                )
            else:
                try:
                    await aiter.aclose()
                except Exception:
                    pass
            raise asyncio.TimeoutError(f"Turn exceeded {timeout}s timeout")
        yield item


async def _drain_after_timeout(
    aiter: AsyncIterator,
    on_result: Callable,
    extra_timeout: float,
) -> None:
    """Continue consuming an event stream after timeout and deliver collected results."""
    deadline = asyncio.get_event_loop().time() + extra_timeout
    collected_outputs: list[str] = []
    final_response = ""
    try:
        while asyncio.get_event_loop().time() < deadline:
            try:
                item = await asyncio.wait_for(aiter.__anext__(), timeout=60)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                continue
            if not isinstance(item, dict) or "_progress" not in item:
                continue
            for progress in item.get("_progress", []):
                stage = progress.get("stage")
                status = progress.get("status", "")
                if stage == "skill" and status in ("completed", "failed"):
                    # Skip internal resource-loading tools — their output
                    # is framework-internal (contains [PIN] markers, full
                    # SKILL.md content) and must not be forwarded to the user.
                    skill_info = progress.get("skill_info") or {}
                    skill_name = skill_info.get("name") or progress.get("tool_name") or ""
                    if skill_name in ("_load_resource_skill", "_load_skill_resource"):
                        continue
                    output = (
                        progress.get("answer")
                        or progress.get("block_answer")
                        or progress.get("output")
                        or ""
                    )
                    if output:
                        collected_outputs.append(output)
                elif stage == "llm":
                    delta = progress.get("delta", "")
                    answer = progress.get("answer", "")
                    if delta:
                        final_response += delta
                    elif answer:
                        final_response = answer
    except Exception as e:
        logger.warning("Deferred drain error: %s", e)
    finally:
        try:
            await aiter.aclose()
        except Exception:
            pass

    # Prefer the LLM's final text response — it's already formatted for the user.
    # Tool outputs are raw JSON and should only be included as fallback when
    # the LLM produced no text (e.g. timeout during tool execution, before LLM reply).
    if final_response.strip():
        result = final_response.strip()
    elif collected_outputs:
        # Fallback: summarize tool outputs instead of dumping raw JSON
        result = "\n\n".join(collected_outputs)
    else:
        result = ""
    # Strip any leaked [PIN] markers (dolphin framework internal token)
    result = result.replace("[PIN]", "").strip()
    if not result.strip():
        return
    if len(result) > 8000:
        result = result[:8000] + f"\n\n... [truncated, total {len(result)} chars]"
    try:
        res = on_result(result)
        if asyncio.iscoroutine(res):
            await res
    except Exception as e:
        logger.warning("Failed to deliver deferred result: %s", e)
