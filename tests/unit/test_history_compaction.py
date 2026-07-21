"""Unit tests for long-session history compaction policy (#166).

Covers design plan U1–U10: threshold gate, tool-safe window, summary merge,
summary failure → safe trim, kept_original, over-budget, config priority,
events, and tool-history regression.
"""

from __future__ import annotations

import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.everbot.core.session.history_compaction import (
    CompactionResult,
    HistoryCompactionConfig,
    HistoryCompactionPolicy,
    find_safe_window_start,
    looks_like_summary_error,
    resolve_history_compaction_config,
    truncate_summary,
    validate_tool_pairing,
)
from src.everbot.core.session.history_utils import _estimate_tokens
from src.everbot.core.session.compressor import SUMMARY_TAG, inject_summary


# ── Factories ────────────────────────────────────────────────────────


def _user(content: str) -> dict:
    return {"role": "user", "content": content}


def _assistant(content: str, tool_calls=None) -> dict:
    msg = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return msg


def _tool(tcid: str, content: str = "ok") -> dict:
    return {"role": "tool", "tool_call_id": tcid, "content": content}


def _tc(tcid: str, name: str = "search", args: str = "{}"):
    return {
        "id": tcid,
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


def _bulk_history(n_pairs: int, payload: str = "x" * 300) -> list:
    """Build n_pairs user/assistant pairs with large content (~tokens each)."""
    msgs = []
    for i in range(n_pairs):
        msgs.append(_user(f"user-{i} {payload}"))
        msgs.append(_assistant(f"asst-{i} {payload}"))
    return msgs


# ── U1: token threshold gate ─────────────────────────────────────────


class TestThresholdGate:
    @pytest.mark.asyncio
    async def test_under_trigger_unchanged(self):
        history = _bulk_history(2, payload="hi")
        cfg = HistoryCompactionConfig(enabled=True, trigger_tokens=40_000)
        summarize = AsyncMock(return_value="summary")
        result = await HistoryCompactionPolicy().ensure_within_budget(
            history, cfg, summarize=summarize
        )
        assert result.changed is False
        assert result.outcome == "skipped"
        assert result.reason == "under_trigger"
        assert result.history == history
        summarize.assert_not_called()

    @pytest.mark.asyncio
    async def test_over_trigger_enters_compress_path(self):
        # ~ (300*2 + overhead) * n pairs → exceed 500 tokens easily
        history = _bulk_history(40, payload="p" * 200)
        assert _estimate_tokens(history) > 500
        cfg = HistoryCompactionConfig(
            enabled=True,
            trigger_tokens=500,
            target_recent_tokens=200,
        )
        summarize = AsyncMock(return_value="compressed facts and open tasks")
        result = await HistoryCompactionPolicy().ensure_within_budget(
            history, cfg, summarize=summarize
        )
        assert result.changed is True
        assert result.outcome == "summarized"
        assert result.after_tokens < result.before_tokens
        assert result.after_tokens <= result.before_tokens * 0.7  # ≥30% drop
        summarize.assert_called_once()
        assert SUMMARY_TAG in result.history[0]["content"]


# ── U2 / U10: tool-safe window ───────────────────────────────────────


class TestSafeWindowToolBoundary:
    def test_window_start_expands_to_assistant_tool_call(self):
        history = [
            _user("early constraint: never delete files"),
            _assistant("ok"),
            _user("search docs"),
            _assistant("calling", tool_calls=[_tc("c1")]),
            _tool("c1", "big result " * 100),
            _assistant("done"),
            _user("recent q"),
            _assistant("recent a"),
        ]
        # Force cut that would land on tool if naive
        start = find_safe_window_start(history, token_budget=50)
        window = history[start:]
        assert validate_tool_pairing(window) == []
        # If tool is in window, owning assistant must be too
        for i, m in enumerate(window):
            if m.get("role") == "tool":
                assert any(
                    m.get("tool_call_id") in [
                        tc.get("id") for tc in (w.get("tool_calls") or [])
                    ]
                    for w in window[:i]
                    if w.get("role") == "assistant"
                )

    def test_compress_with_tool_history_keeps_pairing(self):
        """U10 / S3: tool chain spanning cut stays valid after summary."""
        old = _bulk_history(30, payload="old " * 80)
        tool_block = [
            _user("use tool"),
            _assistant("run", tool_calls=[_tc("t1"), _tc("t2")]),
            _tool("t1", "r1 " * 50),
            _tool("t2", "r2 " * 50),
            _assistant("both done"),
            _user("latest"),
            _assistant("latest reply"),
        ]
        history = old + tool_block
        start = find_safe_window_start(history, token_budget=400)
        window = history[start:]
        assert validate_tool_pairing(window) == []
        # Recent messages retained
        assert window[-1]["content"] == "latest reply"


# ── U3: unfinished tool chain at tail ────────────────────────────────


class TestUnfinishedToolChain:
    def test_incomplete_chain_retained_in_window(self):
        history = _bulk_history(20, payload="z" * 100) + [
            _user("do work"),
            _assistant("calling", tool_calls=[_tc("open1")]),
            # no tool result yet — unfinished
        ]
        start = find_safe_window_start(history, token_budget=300)
        window = history[start:]
        assert any(
            m.get("role") == "assistant" and m.get("tool_calls") for m in window
        )
        assert validate_tool_pairing(window) == []  # open pending at end is OK
        # tool_call must not be orphaned by cutting mid-chain
        assert window[-1].get("role") == "assistant"


# ── U4: existing summary merge ───────────────────────────────────────


class TestExistingSummaryMerge:
    @pytest.mark.asyncio
    async def test_only_one_summary_pair_and_old_text_passed(self):
        summary_pair = inject_summary("旧摘要内容", [])
        rest = _bulk_history(35, payload="m" * 150)
        history = summary_pair + rest
        cfg = HistoryCompactionConfig(
            trigger_tokens=200, target_recent_tokens=150, max_summary_tokens=500
        )
        summarize = AsyncMock(return_value="更新后的完整摘要")
        result = await HistoryCompactionPolicy().ensure_within_budget(
            history, cfg, summarize=summarize
        )
        assert result.changed is True
        # Only one SUMMARY_TAG user message
        tags = [
            m
            for m in result.history
            if m.get("role") == "user"
            and isinstance(m.get("content"), str)
            and SUMMARY_TAG in m["content"]
        ]
        assert len(tags) == 1
        # Old summary text fed into summarize
        assert summarize.call_args[0][0] == "旧摘要内容"


# ── U5: summary failure → safe trim ──────────────────────────────────


class TestSummaryFailureSafeTrim:
    @pytest.mark.asyncio
    async def test_summary_raise_then_window_trimmed(self):
        history = _bulk_history(40, payload="q" * 200)
        cfg = HistoryCompactionConfig(trigger_tokens=300, target_recent_tokens=200)
        summarize = AsyncMock(side_effect=RuntimeError("LLM down"))
        result = await HistoryCompactionPolicy().ensure_within_budget(
            history, cfg, summarize=summarize
        )
        assert result.changed is True
        assert result.outcome in ("window_trimmed", "over_budget_unavoidable")
        assert result.after_tokens < result.before_tokens
        assert validate_tool_pairing(result.history) == []

    @pytest.mark.asyncio
    async def test_error_string_summary_rejected(self):
        history = _bulk_history(40, payload="q" * 200)
        cfg = HistoryCompactionConfig(trigger_tokens=300, target_recent_tokens=200)
        summarize = AsyncMock(return_value="oneshot LLM HTTP 500: boom")
        result = await HistoryCompactionPolicy().ensure_within_budget(
            history, cfg, summarize=summarize
        )
        # Must not inject error as summary body
        for m in result.history:
            if m.get("role") == "assistant" and isinstance(m.get("content"), str):
                assert "oneshot LLM HTTP" not in m["content"]
        assert result.outcome in (
            "window_trimmed",
            "over_budget_unavoidable",
            "kept_original",
        )


# ── U6: cannot safely reduce ─────────────────────────────────────────


class TestKeptOriginal:
    @pytest.mark.asyncio
    async def test_minimal_history_kept_when_trim_impossible(self):
        # Two-message history forced over trigger; each message alone exceeds target.
        # Safe trim must not empty history or leave an illegal sequence.
        history = [
            _user("constraint " + "X" * 3000),
            _assistant("ack " + "Y" * 3000),
        ]
        cfg = HistoryCompactionConfig(
            trigger_tokens=10,  # force trigger
            target_recent_tokens=5,
        )
        summarize = AsyncMock(side_effect=RuntimeError("no"))
        result = await HistoryCompactionPolicy().ensure_within_budget(
            history, cfg, summarize=summarize
        )
        assert len(result.history) >= 1
        assert validate_tool_pairing(result.history) == []
        # Must not silently drop everything
        assert any(
            isinstance(m.get("content"), str) and m["content"]
            for m in result.history
        )
        assert result.outcome in (
            "kept_original",
            "over_budget_unavoidable",
            "window_trimmed",
        )
        if result.outcome == "kept_original":
            assert result.changed is False


# ── U7: single oversized message ─────────────────────────────────────


class TestOverBudgetUnavoidable:
    @pytest.mark.asyncio
    async def test_single_huge_tool_json_not_truncated(self):
        huge_args = '{"data":"' + ("Z" * 50_000) + '"}'
        history = [
            _user("run"),
            _assistant("call", tool_calls=[_tc("big1", "write", huge_args)]),
            _tool("big1", "done"),
            _assistant("finished"),
        ]
        cfg = HistoryCompactionConfig(trigger_tokens=100, target_recent_tokens=50)
        summarize = AsyncMock(side_effect=RuntimeError("skip"))
        result = await HistoryCompactionPolicy().ensure_within_budget(
            history, cfg, summarize=summarize
        )
        # Tool arguments must remain intact if messages kept
        for m in result.history:
            if m.get("tool_calls"):
                args = m["tool_calls"][0]["function"]["arguments"]
                assert args == huge_args
                assert "Z" * 1000 in args
        assert result.outcome in (
            "over_budget_unavoidable",
            "kept_original",
            "window_trimmed",
        )


# ── U8: config priority ──────────────────────────────────────────────


class TestConfigResolve:
    def test_defaults(self):
        cfg = resolve_history_compaction_config(None)
        assert cfg.enabled is True
        assert cfg.trigger_tokens == 40_000
        assert cfg.target_recent_tokens == 20_000
        assert cfg.max_summary_tokens == 2_000

    def test_agent_overrides_global(self):
        config = {
            "everbot": {
                "session": {
                    "history_compaction": {
                        "trigger_tokens": 30_000,
                        "target_recent_tokens": 15_000,
                    }
                },
                "agents": {
                    "demo_agent": {
                        "session": {
                            "history_compaction": {
                                "trigger_tokens": 25_000,
                            }
                        }
                    }
                },
            }
        }
        cfg = resolve_history_compaction_config(config, "demo_agent")
        assert cfg.trigger_tokens == 25_000
        assert cfg.target_recent_tokens == 15_000

    @pytest.mark.asyncio
    async def test_enabled_false_skips_summarize(self):
        history = _bulk_history(40, payload="p" * 200)
        cfg = HistoryCompactionConfig(enabled=False, trigger_tokens=10)
        summarize = AsyncMock(return_value="x")
        result = await HistoryCompactionPolicy().ensure_within_budget(
            history, cfg, summarize=summarize
        )
        assert result.outcome == "skipped"
        assert result.reason == "disabled"
        assert result.changed is False
        summarize.assert_not_called()

    def test_invalid_values_fallback(self):
        config = {
            "everbot": {
                "session": {
                    "history_compaction": {
                        "trigger_tokens": 5,  # too small
                        "max_summary_tokens": 99999,
                    }
                }
            }
        }
        cfg = resolve_history_compaction_config(config)
        assert cfg.trigger_tokens == 40_000
        assert cfg.max_summary_tokens == 2_000


# ── U9: event payload ────────────────────────────────────────────────


class TestEventPayload:
    def test_event_fields_no_body(self):
        r = CompactionResult(
            history=[{"role": "user", "content": "SECRET_BODY"}],
            changed=True,
            outcome="summarized",
            reason="over_trigger",
            before_tokens=90000,
            after_tokens=20000,
            summary_tokens=400,
            retained_messages=42,
        )
        payload = r.to_event_payload(provider="MilkieProvider", session_id="s1")
        assert payload["type"] == "history_compaction"
        assert payload["before_tokens"] == 90000
        assert payload["after_tokens"] == 20000
        assert payload["outcome"] == "summarized"
        assert payload["summary_tokens"] == 400
        assert payload["retained_messages"] == 42
        assert payload["provider"] == "MilkieProvider"
        assert payload["session_id"] == "s1"
        blob = str(payload)
        assert "SECRET_BODY" not in blob


# ── Helpers ──────────────────────────────────────────────────────────


class TestHelpers:
    def test_validate_orphan_tool(self):
        msgs = [_user("hi"), _tool("missing", "x")]
        errs = validate_tool_pairing(msgs)
        assert any(e.startswith("orphan_tool:") for e in errs)

    def test_validate_complete_chain(self):
        msgs = [
            _user("hi"),
            _assistant("x", tool_calls=[_tc("a")]),
            _tool("a"),
            _assistant("done"),
        ]
        assert validate_tool_pairing(msgs) == []

    def test_looks_like_summary_error(self):
        assert looks_like_summary_error("") is True
        assert looks_like_summary_error("oneshot LLM HTTP 500: x") is True
        assert looks_like_summary_error("用户要求完成任务") is False

    def test_truncate_summary(self):
        text = "字" * 1000
        out = truncate_summary(text, max_summary_tokens=10)  # 30 chars
        assert len(out) <= 30
        assert out.endswith("…")


# ── S1 quantitative fixture ──────────────────────────────────────────


class TestS1TokenReduction:
    @pytest.mark.asyncio
    async def test_fixture_drops_at_least_30_percent(self):
        """Reproducible large history: early constraint + bulk + tool chain."""
        early = [
            _user("CRITICAL_CONSTRAINT: always use Python 3.11 and never rm -rf"),
            _assistant("Understood, I will follow that constraint."),
        ]
        bulk = _bulk_history(80, payload=("history filler " * 40))
        tools = [
            _user("search the codebase"),
            _assistant("searching", tool_calls=[_tc("s1", "grep", '{"q":"foo"}')]),
            _tool("s1", "match line " * 200),
            _assistant("found matches"),
            _user("continue with the open task"),
            _assistant("working on open task now"),
        ]
        history = early + bulk + tools
        baseline = _estimate_tokens(history)
        assert baseline > 10_000

        cfg = HistoryCompactionConfig(
            trigger_tokens=5_000,
            target_recent_tokens=3_000,
            max_summary_tokens=500,
        )
        summarize = AsyncMock(
            return_value=(
                "User CRITICAL_CONSTRAINT: always use Python 3.11 and never rm -rf. "
                "Open task: continue codebase search. Prior work summarized."
            )
        )
        result = await HistoryCompactionPolicy().ensure_within_budget(
            history, cfg, summarize=summarize
        )
        assert result.changed is True
        drop = (baseline - result.after_tokens) / baseline
        assert drop >= 0.30, f"expected ≥30% drop, got {drop:.1%} ({baseline}→{result.after_tokens})"
        assert validate_tool_pairing(result.history) == []
        # Constraint preserved in summary or recent window
        blob = " ".join(
            (m.get("content") or "")
            for m in result.history
            if isinstance(m.get("content"), str)
        )
        assert "CRITICAL_CONSTRAINT" in blob or "Python 3.11" in blob
        assert "open task" in blob.lower() or "working on open task" in blob


# ── SessionManager orchestration (light) ─────────────────────────────


class TestSessionManagerCompact:
    @pytest.mark.asyncio
    async def test_disabled_config_no_import(self, tmp_path):
        from src.everbot.core.session.session import SessionManager

        sm = SessionManager(tmp_path)
        agent = MagicMock()
        agent.name = "demo"
        agent.context_id = "sess1"

        mock_provider = MagicMock()
        mock_provider.export_session.return_value = {
            "history_messages": _bulk_history(50, payload="p" * 200),
            "variables": {},
        }
        mock_provider.import_session = MagicMock()

        with patch(
            "src.everbot.core.agent.provider.provider_for", return_value=mock_provider
        ):
            result = await sm.maybe_compact_session_history(
                agent,
                "sess1",
                "demo",
                config={
                    "everbot": {
                        "session": {"history_compaction": {"enabled": False}}
                    }
                },
            )
        assert result.outcome == "skipped"
        mock_provider.export_session.assert_not_called()
        mock_provider.import_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_export_timeout_keeps_original_and_returns(self, tmp_path, monkeypatch):
        """Stalled provider export must not hang forever (F-007)."""
        import src.everbot.core.session.session as session_mod
        from src.everbot.core.session.session import SessionManager

        # Fail fast in tests
        monkeypatch.setattr(
            session_mod, "_COMPACTION_PROVIDER_IO_TIMEOUT_SECONDS", 0.05
        )

        sm = SessionManager(tmp_path)
        agent = MagicMock()
        agent.name = "demo"
        agent.context_id = "sess1"

        def _hang(_agent):
            import time

            time.sleep(2.0)
            return {"history_messages": [], "variables": {}}

        mock_provider = MagicMock()
        mock_provider.export_session.side_effect = _hang
        mock_provider.import_session = MagicMock()

        with patch(
            "src.everbot.core.agent.provider.provider_for", return_value=mock_provider
        ):
            t0 = time.monotonic()
            result = await sm.maybe_compact_session_history(
                agent,
                "sess1",
                "demo",
                config={
                    "everbot": {
                        "session": {
                            "history_compaction": {
                                "enabled": True,
                                "trigger_tokens": 1000,
                                "target_recent_tokens": 500,
                            }
                        }
                    }
                },
            )
            elapsed = time.monotonic() - t0

        assert result.outcome == "kept_original"
        assert result.reason == "export_timeout"
        assert elapsed < 1.0, f"must not block on hung export; elapsed={elapsed:.2f}s"
        mock_provider.import_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_apply_and_timeline_on_success(self, tmp_path):
        from src.everbot.core.session.session import SessionManager

        sm = SessionManager(tmp_path)
        agent = MagicMock()
        agent.name = "demo"
        agent.context_id = "sess1"
        agent.executor = MagicMock()
        agent.executor.context = None

        history = _bulk_history(80, payload=("p" * 400))
        mock_provider = MagicMock()
        mock_provider.export_session.return_value = {
            "history_messages": history,
            "variables": {},
        }
        mock_provider.import_session = MagicMock()

        with patch(
            "src.everbot.core.agent.provider.provider_for", return_value=mock_provider
        ), patch(
            "src.everbot.core.session.session.SessionCompressor._generate_summary",
            new_callable=AsyncMock,
            return_value="summary of old work and constraints",
        ), patch.object(
            sm, "update_atomic", new_callable=AsyncMock, return_value=MagicMock()
        ):
            result = await sm.maybe_compact_session_history(
                agent,
                "sess1",
                "demo",
                config={
                    "everbot": {
                        "session": {
                            "history_compaction": {
                                "enabled": True,
                                "trigger_tokens": 2000,
                                "target_recent_tokens": 1000,
                            }
                        }
                    }
                },
            )

        assert result.changed is True
        mock_provider.import_session.assert_called_once()
        events = sm.get_timeline("sess1")
        assert any(e.get("type") == "history_compaction" for e in events)
        ev = next(e for e in events if e.get("type") == "history_compaction")
        assert "before_tokens" in ev and "after_tokens" in ev
        assert "SECRET" not in str(ev)

    @pytest.mark.asyncio
    async def test_persist_under_held_session_lock(self, tmp_path):
        """F-001: pre-turn path holds the in-process lock; compact must not
        re-enter update_atomic. With lock_already_held=True, mirror is written.
        """
        import time as _time
        from src.everbot.core.session.session import SessionManager
        from src.everbot.core.session.session_data import SessionData

        sm = SessionManager(tmp_path)
        session_id = "web_session_demo_agent"
        history = _bulk_history(80, payload=("p" * 400))
        # Seed disk session so update_atomic mutates an existing file.
        seed = SessionData(
            session_id=session_id,
            agent_name="demo",
            model_name="gpt-4",
            session_type="channel",
            history_messages=list(history),
            variables={},
        )
        await sm.persistence.save_data(seed)

        agent = MagicMock()
        agent.name = "demo"
        agent.context_id = session_id
        agent.executor = MagicMock()
        agent.executor.context = None

        live_history = {"msgs": list(history)}

        class _RecordingProvider:
            def export_session(self, _agent):
                return {"history_messages": list(live_history["msgs"]), "variables": {}}

            def import_session(self, _agent, portable_state):
                live_history["msgs"] = list(portable_state["history_messages"])

        provider = _RecordingProvider()
        lock = sm._get_lock(session_id)
        await lock.acquire()
        try:
            t0 = _time.monotonic()
            with patch(
                "src.everbot.core.agent.provider.provider_for", return_value=provider
            ), patch(
                "src.everbot.core.session.session.SessionCompressor._generate_summary",
                new_callable=AsyncMock,
                return_value="summary of old work; CRITICAL_CONSTRAINT use Python 3.11",
            ):
                result = await sm.maybe_compact_session_history(
                    agent,
                    session_id,
                    "demo",
                    config={
                        "everbot": {
                            "session": {
                                "history_compaction": {
                                    "enabled": True,
                                    "trigger_tokens": 2000,
                                    "target_recent_tokens": 1000,
                                }
                            }
                        }
                    },
                    lock_already_held=True,
                )
            elapsed = _time.monotonic() - t0
        finally:
            lock.release()

        assert result.changed is True
        assert result.outcome != "persist_failed"
        # Must not burn the 10s lock timeout (F-001 regression).
        assert elapsed < 3.0, f"compact under held lock took {elapsed:.1f}s"
        # Live history is the compacted set used by the next LLM call (S1/S4).
        assert len(live_history["msgs"]) < len(history)
        drop = (result.before_tokens - result.after_tokens) / max(result.before_tokens, 1)
        assert drop >= 0.30
        loaded = await sm.load_session(session_id)
        assert loaded is not None
        assert loaded.history_messages == live_history["msgs"]
        assert loaded.variables.get("_history_compaction", {}).get("outcome") == result.outcome

    @pytest.mark.asyncio
    async def test_milkie_import_chat_first_request_and_tool_round(self, tmp_path):
        """S1/S3/S4 F-002: real MilkieProvider path — export → compact →
        /session/import → /chat first request uses reduced base; tool round OK.

        Uses a controllable FakeMilkieServe (httpx MockTransport) so the test
        exercises import_session rewrite + run_turn /chat, not in-memory stubs.
        """
        import json
        import httpx

        from src.everbot.core.agent.provider.milkie.provider import (
            MilkieAgentHandle,
            MilkieProvider,
        )
        from src.everbot.core.session.session import SessionManager
        from src.everbot.core.session.session_data import SessionData

        early = [
            _user("CRITICAL_CONSTRAINT: always use Python 3.11"),
            _assistant("Understood, I will use Python 3.11."),
        ]
        bulk = _bulk_history(60, payload="payload-" + ("z" * 350))
        tool_tail = [
            _user("search the repo for open task"),
            _assistant(
                "searching", tool_calls=[_tc("tc_live", "search", '{"q":"open"}')]
            ),
            _tool("tc_live", "found files A B C"),
            _assistant("working on open task with results"),
        ]
        history = early + bulk + tool_tail
        baseline = _estimate_tokens(history)
        assert baseline > 5000

        def _to_milkie_messages(msgs):
            out = []
            for m in msgs:
                role = m.get("role")
                if role == "user":
                    out.append(
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": m.get("content") or ""}
                            ],
                        }
                    )
                elif role == "assistant":
                    content = []
                    if m.get("content"):
                        content.append({"type": "text", "text": m["content"]})
                    for tc in m.get("tool_calls") or []:
                        fn = (tc or {}).get("function") or {}
                        args = fn.get("arguments") or "{}"
                        try:
                            inp = json.loads(args) if isinstance(args, str) else args
                        except Exception:
                            inp = {}
                        content.append(
                            {
                                "type": "tool_use",
                                "id": tc.get("id"),
                                "name": fn.get("name"),
                                "input": inp,
                            }
                        )
                    out.append({"role": "assistant", "content": content})
                elif role == "tool":
                    out.append(
                        {
                            "role": "tool",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": m.get("tool_call_id"),
                                    "content": m.get("content") or "",
                                }
                            ],
                        }
                    )
            return out

        def _portable_from_history(msgs, context_id="c1", run_id="run-old"):
            regions = [
                {
                    "id": "header",
                    "section": "header",
                    "content": {"text": "system"},
                }
            ]
            for i, m in enumerate(msgs):
                if m.get("role") != "user":
                    continue
                # Pair with following assistant text (tools folded in export path)
                asst = ""
                j = i + 1
                while j < len(msgs) and msgs[j].get("role") != "user":
                    cur = msgs[j]
                    if cur.get("role") == "assistant" and isinstance(
                        cur.get("content"), str
                    ):
                        asst += (cur.get("content") or "") + " "
                    j += 1
                regions.append(
                    {
                        "id": f"history:turn-{i}",
                        "section": "history",
                        "target": "message",
                        "content": {
                            "userInput": m.get("content") or "",
                            "assistantText": asst.strip(),
                        },
                    }
                )
            return {
                "manifest": {
                    "schemaVersion": 1,
                    "contextId": context_id,
                    "latestRunId": run_id,
                    "exportedAt": 1_700_000_000_000,
                },
                "events": [
                    {
                        "id": "evt-start",
                        "type": "agent.run.started",
                        "runId": run_id,
                        "payload": {
                            "previousRunId": "run-prev",
                            "contextId": context_id,
                        },
                    },
                    {
                        "id": "evt-cp",
                        "type": "agent.checkpoint",
                        "runId": run_id,
                        "payload": {
                            "checkpoint": {
                                "checkpointId": "cp-1",
                                "context": {
                                    "regions": {"epoch": 1, "regions": regions}
                                },
                            }
                        },
                    },
                ],
                "variables": {},
            }

        def _history_from_portable(session: dict) -> list:
            """Recover approximate alfred history from imported region pairs."""
            msgs = []
            for event in session.get("events") or []:
                if event.get("type") != "agent.checkpoint":
                    continue
                regions = (
                    ((event.get("payload") or {}).get("checkpoint") or {})
                    .get("context", {})
                    .get("regions", {})
                )
                region_list = (
                    regions.get("regions") if isinstance(regions, dict) else regions
                ) or []
                for r in region_list:
                    if not str(r.get("id") or "").startswith("history:turn"):
                        continue
                    content = r.get("content") or {}
                    user = content.get("userInput") or ""
                    asst = content.get("assistantText") or ""
                    msgs.append(_user(user))
                    # Unfold tool prose folded by milkie rewrite when present.
                    if "[tool_call" in asst or "[tool_result" in asst:
                        msgs.append(_assistant(asst))
                    else:
                        msgs.append(_assistant(asst))
            return msgs

        class FakeMilkieServe:
            """In-process milkie serve: history/export/import/chat."""

            def __init__(self, seed_history):
                self.history = list(seed_history)
                self.chat_history_snapshots: list = []
                self.chat_calls = 0
                self.imported: list = []

            def handler(self, request: httpx.Request) -> httpx.Response:
                path = request.url.path
                body = json.loads(request.content) if request.content else {}
                if path.endswith("/session/history"):
                    return httpx.Response(
                        200,
                        json={"messages": _to_milkie_messages(self.history)},
                    )
                if path.endswith("/session/export"):
                    return httpx.Response(
                        200,
                        json=_portable_from_history(
                            self.history, context_id=body.get("contextId") or "c1"
                        ),
                    )
                if path.endswith("/session/import"):
                    session = body.get("session") or {}
                    self.imported.append(session)
                    self.history = _history_from_portable(session)
                    return httpx.Response(200, json={"ok": True})
                if path.endswith("/chat"):
                    # First LLM request base = live serve history after import.
                    self.chat_history_snapshots.append(list(self.history))
                    self.chat_calls += 1
                    user_input = body.get("input") or ""
                    # Simulate serve appending the turn (multi-tool continue).
                    self.history.append(_user(user_input))
                    if self.chat_calls == 1:
                        sse = "".join(
                            f"event: {ev}\ndata: {json.dumps(d)}\n\n"
                            for ev, d in [
                                ("agent.run.started", {"contextId": "c1"}),
                                ("message_delta", {"text": "ack after compact"}),
                                (
                                    "agent.run.completed",
                                    {
                                        "status": "completed",
                                        "output": "ack after compact",
                                    },
                                ),
                            ]
                        )
                        self.history.append(_assistant("ack after compact"))
                    else:
                        # Tool round continuation.
                        tcid = "tc_next"
                        self.history.append(
                            _assistant(
                                "calling",
                                tool_calls=[_tc(tcid, "read_file", "{}")],
                            )
                        )
                        self.history.append(_tool(tcid, "file contents ok"))
                        self.history.append(_assistant("done with tool"))
                        sse = "".join(
                            f"event: {ev}\ndata: {json.dumps(d)}\n\n"
                            for ev, d in [
                                ("agent.run.started", {"contextId": "c1"}),
                                ("message_delta", {"text": "done with tool"}),
                                (
                                    "agent.run.completed",
                                    {
                                        "status": "completed",
                                        "output": "done with tool",
                                    },
                                ),
                            ]
                        )
                    return httpx.Response(
                        200,
                        headers={"content-type": "text/event-stream"},
                        content=sse.encode("utf-8"),
                    )
                return httpx.Response(404, json={"error": f"unexpected {path}"})

        serve = FakeMilkieServe(history)
        transport = httpx.MockTransport(serve.handler)
        sync_client = httpx.Client(transport=transport)
        async_client = httpx.AsyncClient(transport=transport)
        provider = MilkieProvider(
            "http://sidecar", client=async_client, sync_client=sync_client
        )

        sm = SessionManager(tmp_path)
        session_id = "web_session_demo_agent"
        seed = SessionData(
            session_id=session_id,
            agent_name="demo",
            model_name="gpt-4",
            session_type="channel",
            history_messages=list(history),
            variables={},
        )
        await sm.persistence.save_data(seed)

        agent = MilkieAgentHandle(
            base_url="http://sidecar", context_id="c1", name="demo"
        )

        try:
            with patch(
                "src.everbot.core.agent.provider.provider_for", return_value=provider
            ), patch(
                "src.everbot.core.session.session.SessionCompressor._generate_summary",
                new_callable=AsyncMock,
                return_value=(
                    "CRITICAL_CONSTRAINT: always use Python 3.11. "
                    "Open task: continue codebase search."
                ),
            ):
                result = await sm.maybe_compact_session_history(
                    agent,
                    session_id,
                    "demo",
                    config={
                        "everbot": {
                            "session": {
                                "history_compaction": {
                                    "enabled": True,
                                    "trigger_tokens": 2000,
                                    "target_recent_tokens": 1500,
                                }
                            }
                        }
                    },
                    lock_already_held=False,
                )

            assert result.changed is True
            assert serve.imported, "must POST /session/import before chat"
            # Live serve history is the compacted base (not the bulk filler).
            after_base = _estimate_tokens(serve.history)
            drop = (baseline - after_base) / baseline
            assert drop >= 0.30, f"expected ≥30% drop, got {drop:.1%}"
            blob = " ".join(
                (m.get("content") or "")
                for m in serve.history
                if isinstance(m.get("content"), str)
            )
            assert "CRITICAL_CONSTRAINT" in blob or "Python 3.11" in blob
            assert "payload-" not in blob or len(serve.history) < len(history)

            # First /chat uses post-import history as LLM base (S1/S4).
            events = [
                e
                async for e in provider.run_turn(
                    agent, "continue after compact"
                )
            ]
            assert events
            assert serve.chat_history_snapshots
            first_req = serve.chat_history_snapshots[0]
            first_tokens = _estimate_tokens(first_req)
            drop2 = (baseline - first_tokens) / baseline
            assert drop2 >= 0.30, f"first /chat base drop {drop2:.1%}"
            assert validate_tool_pairing(result.history) == []

            # Second /chat = tool-style continue on compacted session (S3).
            events2 = [
                e async for e in provider.run_turn(agent, "read the next file")
            ]
            assert events2
            assert validate_tool_pairing(serve.history) == []
            assert any(
                m.get("role") == "tool" and m.get("tool_call_id") == "tc_next"
                for m in serve.history
            )

            loaded = await sm.load_session(session_id)
            assert loaded is not None
            assert loaded.variables.get("_history_compaction", {}).get("outcome") == (
                result.outcome
            )
        finally:
            sync_client.close()
            await async_client.aclose()


# ── F-004: full-region summary coverage ───────────────────────────────


class TestSummaryRegionCoverage:
    def test_format_includes_late_constraint_and_tool_fact(self):
        """Prefix bulk must not starve late constraint + tool conclusion (F-004)."""
        from src.everbot.core.session.compressor import _format_messages_for_prompt

        early_noise = _bulk_history(40, payload="NOISE_" + ("n" * 400))
        late = [
            _user("LATE_CONSTRAINT: deploy only to staging"),
            _assistant(
                "checking",
                tool_calls=[_tc("t_late", "env_check", '{"env":"staging"}')],
            ),
            _tool("t_late", "TOOL_CONCLUSION: staging healthy, prod locked"),
            _assistant("will deploy to staging only"),
        ]
        msgs = early_noise + late
        text = _format_messages_for_prompt(msgs, max_chars=12_000)
        assert "LATE_CONSTRAINT" in text
        assert "TOOL_CONCLUSION" in text or "staging healthy" in text

    def test_chunk_messages_covers_full_region(self):
        from src.everbot.core.session.compressor import (
            chunk_messages_for_summary,
            _format_messages_for_prompt,
        )

        early = _bulk_history(30, payload="EARLY_" + ("e" * 300))
        mid = [_user("MID_FACT: deadline is Friday"), _assistant("noted deadline")]
        late = [
            _user("LATE_CONSTRAINT: never force-push main"),
            _assistant("ok"),
            _tool("tx", "TOOL_FACT: branch protected"),
        ]
        # tool without assistant is ok for formatter/chunker unit tests
        msgs = early + mid + late
        chunks = chunk_messages_for_summary(msgs, max_chars_per_chunk=3000)
        assert len(chunks) >= 2
        joined = "\n".join(_format_messages_for_prompt(c, max_chars=50_000) for c in chunks)
        assert "LATE_CONSTRAINT" in joined
        assert "TOOL_FACT" in joined
        # Every source message accounted for across chunks
        assert sum(len(c) for c in chunks) == len(
            [m for m in msgs if isinstance(m, dict)]
        )

    @pytest.mark.asyncio
    async def test_generate_summary_map_reduce_sees_late_facts(self):
        """Map-reduce path: LLM prompts include late-region facts (F-004)."""
        from src.everbot.core.session.compressor import SessionCompressor

        early = _bulk_history(25, payload="BULK_" + ("b" * 350))
        late = [
            _user("LATE_CONSTRAINT: use Python 3.11 only"),
            _assistant(
                "verifying",
                tool_calls=[_tc("tv", "python", '{"v":"3.11"}')],
            ),
            _tool("tv", "TOOL_CONCLUSION: interpreter is 3.11.9"),
            _assistant("confirmed 3.11"),
        ]
        msgs = early + late
        prompts: list[str] = []

        async def fake_llm(_ctx, prompt, **_kw):
            prompts.append(prompt)
            # Echo markers so reduce still carries them
            bits = []
            if "LATE_CONSTRAINT" in prompt:
                bits.append("LATE_CONSTRAINT: use Python 3.11 only")
            if "TOOL_CONCLUSION" in prompt:
                bits.append("TOOL_CONCLUSION: interpreter is 3.11.9")
            return " ".join(bits) or "partial summary of bulk"

        compressor = SessionCompressor(None)
        with patch(
            "src.everbot.core.agent.provider.oneshot_llm_provider"
        ) as mock_oneshot:
            mock_oneshot.return_value.call_llm = AsyncMock(side_effect=fake_llm)
            summary = await compressor._generate_summary("", msgs)

        assert prompts, "must call oneshot LLM"
        all_prompts = "\n".join(prompts)
        assert "LATE_CONSTRAINT" in all_prompts
        assert "TOOL_CONCLUSION" in all_prompts or "3.11.9" in all_prompts
        assert "LATE_CONSTRAINT" in summary or "Python 3.11" in summary

    @pytest.mark.asyncio
    async def test_map_reduce_covers_more_than_eight_chunks_last_chunk_fact(self):
        """F-004: >8 map chunks must still process the last chunk (no silent drop).

        Reviewer repro: ~30 × 3k-char messages → 10 chunks; critical fact in
        chunk index 9 must enter a map prompt and survive into the final summary.
        """
        from src.everbot.core.session.compressor import (
            SessionCompressor,
            chunk_messages_for_summary,
            _SUMMARY_CHUNK_CHARS,
        )

        msgs = [
            _user(f"MSG_{i:02d} " + ("x" * 3000)) for i in range(29)
        ]
        msgs.append(
            _user(
                "CRITICAL_LAST_CONSTRAINT: only Python 3.11 "
                + ("y" * 2000)
            )
        )
        msgs.append(
            _assistant(
                "verifying last fact",
                tool_calls=[_tc("t_last", "check", '{"v":"3.11"}')],
            )
        )
        msgs.append(_tool("t_last", "TOOL_LAST_FACT: interpreter 3.11.9 confirmed"))

        chunks = chunk_messages_for_summary(
            msgs, max_chars_per_chunk=_SUMMARY_CHUNK_CHARS
        )
        assert len(chunks) > 8, f"need >8 chunks for F-004 repro, got {len(chunks)}"
        last_chunk_text = "\n".join(
            (m.get("content") or "") for m in chunks[-1]
        )
        assert "CRITICAL_LAST_CONSTRAINT" in last_chunk_text

        map_prompts: list[str] = []
        reduce_prompts: list[str] = []
        map_calls = 0

        async def fake_llm(_ctx, prompt, **_kw):
            nonlocal map_calls
            # Map prompts contain raw dialogue lines; reduce uses [分段摘要...]
            if "[分段摘要" in prompt:
                reduce_prompts.append(prompt)
                bits = []
                if "CRITICAL_LAST_CONSTRAINT" in prompt:
                    bits.append("CRITICAL_LAST_CONSTRAINT: only Python 3.11")
                if "TOOL_LAST_FACT" in prompt or "3.11.9" in prompt:
                    bits.append("TOOL_LAST_FACT: interpreter 3.11.9")
                return " ".join(bits) or "merged partial summaries"
            map_prompts.append(prompt)
            map_calls += 1
            bits = []
            if "CRITICAL_LAST_CONSTRAINT" in prompt:
                bits.append("CRITICAL_LAST_CONSTRAINT: only Python 3.11")
            if "TOOL_LAST_FACT" in prompt:
                bits.append("TOOL_LAST_FACT: interpreter 3.11.9")
            return " ".join(bits) or "partial map summary bulk chunk"

        compressor = SessionCompressor(None)
        with patch(
            "src.everbot.core.agent.provider.oneshot_llm_provider"
        ) as mock_oneshot:
            mock_oneshot.return_value.call_llm = AsyncMock(side_effect=fake_llm)
            summary = await compressor._generate_summary("", msgs)

        # Every chunk must be mapped — not only the first 8
        assert map_calls == len(chunks), (
            f"map must cover all {len(chunks)} chunks, got {map_calls}"
        )
        all_map = "\n".join(map_prompts)
        assert "CRITICAL_LAST_CONSTRAINT" in all_map
        assert "TOOL_LAST_FACT" in all_map or "3.11.9" in all_map
        # Final summary must carry the late-region fact (via map echo → reduce)
        assert (
            "CRITICAL_LAST_CONSTRAINT" in summary
            or "Python 3.11" in summary
            or "TOOL_LAST_FACT" in summary
        )

    @pytest.mark.asyncio
    async def test_map_subcall_empty_fails_closed_no_partial_summary(self):
        """F-005: any empty map result aborts; no partial summary is returned.

        A late-chunk map returning empty must not let earlier partials be
        reduced and committed, which would silently drop that chunk's facts.
        """
        from src.everbot.core.session.compressor import (
            SessionCompressor,
            chunk_messages_for_summary,
            _SUMMARY_CHUNK_CHARS,
        )

        msgs = [_user(f"MSG_{i:02d} " + ("x" * 3000)) for i in range(12)]
        msgs.append(_user("CRITICAL_FACT_IN_LATE_CHUNK: only use Python 3.11"))
        chunks = chunk_messages_for_summary(
            msgs, max_chars_per_chunk=_SUMMARY_CHUNK_CHARS
        )
        assert len(chunks) >= 3, f"need multi-chunk map, got {len(chunks)}"

        map_calls = 0

        async def fake_llm(_ctx, prompt, **_kw):
            nonlocal map_calls
            if "[分段摘要" in prompt:
                return "merged partial — should never be reached"
            map_calls += 1
            # Fail the last map chunk with empty output (fail-closed).
            if "CRITICAL_FACT_IN_LATE_CHUNK" in prompt:
                return ""
            return f"partial map summary for bulk chunk {map_calls}"

        compressor = SessionCompressor(None)
        with patch(
            "src.everbot.core.agent.provider.oneshot_llm_provider"
        ) as mock_oneshot:
            mock_oneshot.return_value.call_llm = AsyncMock(side_effect=fake_llm)
            with pytest.raises(RuntimeError, match="Map summary failed"):
                await compressor._generate_summary("", msgs)

        assert map_calls == len(chunks), (
            "map must attempt every chunk before abort; "
            f"expected {len(chunks)}, got {map_calls}"
        )

    @pytest.mark.asyncio
    async def test_map_subcall_error_text_fails_closed_no_partial_summary(self):
        """F-005: error-like map output aborts the whole summary generation."""
        from src.everbot.core.session.compressor import (
            SessionCompressor,
            chunk_messages_for_summary,
            _SUMMARY_CHUNK_CHARS,
        )

        msgs = [_user(f"MSG_{i:02d} " + ("y" * 3000)) for i in range(12)]
        chunks = chunk_messages_for_summary(
            msgs, max_chars_per_chunk=_SUMMARY_CHUNK_CHARS
        )
        assert len(chunks) >= 2

        async def fake_llm(_ctx, prompt, **_kw):
            if "[分段摘要" in prompt:
                return "should not reduce"
            # Second map call returns error-like text
            if "MSG_06" in prompt or "MSG_07" in prompt or "MSG_08" in prompt:
                return "oneshot LLM HTTP 500: timeout"
            return "partial map ok"

        compressor = SessionCompressor(None)
        with patch(
            "src.everbot.core.agent.provider.oneshot_llm_provider"
        ) as mock_oneshot:
            mock_oneshot.return_value.call_llm = AsyncMock(side_effect=fake_llm)
            with pytest.raises(RuntimeError, match="Map summary failed"):
                await compressor._generate_summary("", msgs)

    @pytest.mark.asyncio
    async def test_reduce_subcall_empty_fails_closed_no_partial_summary(self):
        """F-005: empty reduce batch aborts; no incomplete merge is returned."""
        from src.everbot.core.session.compressor import (
            SessionCompressor,
            chunk_messages_for_summary,
            _SUMMARY_CHUNK_CHARS,
        )

        # Enough map partials to force at least one multi-partial reduce batch.
        msgs = [_user(f"MSG_{i:02d} " + ("z" * 3000)) for i in range(20)]
        chunks = chunk_messages_for_summary(
            msgs, max_chars_per_chunk=_SUMMARY_CHUNK_CHARS
        )
        assert len(chunks) >= 3

        reduce_calls = 0

        async def fake_llm(_ctx, prompt, **_kw):
            nonlocal reduce_calls
            if "[分段摘要" in prompt:
                reduce_calls += 1
                # First reduce batch fails empty — must not drop and continue.
                return ""
            return f"map partial {prompt[:40]}"

        compressor = SessionCompressor(None)
        with patch(
            "src.everbot.core.agent.provider.oneshot_llm_provider"
        ) as mock_oneshot:
            mock_oneshot.return_value.call_llm = AsyncMock(side_effect=fake_llm)
            with pytest.raises(RuntimeError, match="Reduce summary failed"):
                await compressor._generate_summary("", msgs)

        assert reduce_calls >= 1

    @pytest.mark.asyncio
    async def test_reduce_subcall_error_text_fails_closed_no_partial_summary(self):
        """F-005: error-like reduce output aborts; partials are not committed."""
        from src.everbot.core.session.compressor import (
            SessionCompressor,
            chunk_messages_for_summary,
            _SUMMARY_CHUNK_CHARS,
        )

        msgs = [_user(f"MSG_{i:02d} " + ("w" * 3000)) for i in range(20)]
        chunks = chunk_messages_for_summary(
            msgs, max_chars_per_chunk=_SUMMARY_CHUNK_CHARS
        )
        assert len(chunks) >= 3

        async def fake_llm(_ctx, prompt, **_kw):
            if "[分段摘要" in prompt:
                return "oneshot LLM HTTP 503: service unavailable"
            return "map partial ok"

        compressor = SessionCompressor(None)
        with patch(
            "src.everbot.core.agent.provider.oneshot_llm_provider"
        ) as mock_oneshot:
            mock_oneshot.return_value.call_llm = AsyncMock(side_effect=fake_llm)
            with pytest.raises(RuntimeError, match="Reduce summary failed"):
                await compressor._generate_summary("", msgs)

    @pytest.mark.asyncio
    async def test_map_fail_closed_propagates_to_policy_fallback(self):
        """F-005/S5: map failure via real compressor path → safe trim / keep, not partial summary."""
        from src.everbot.core.session.compressor import SessionCompressor
        from src.everbot.core.session.history_compaction import (
            HistoryCompactionConfig,
            HistoryCompactionPolicy,
        )

        history = _bulk_history(40, payload="q" * 400)
        # Force multi-chunk map by using large compress region.
        cfg = HistoryCompactionConfig(
            trigger_tokens=300, target_recent_tokens=200, max_summary_tokens=500
        )

        map_n = 0

        async def flaky_llm(_ctx, prompt, **_kw):
            nonlocal map_n
            if "[分段摘要" in prompt:
                return "reduce should not matter"
            map_n += 1
            if map_n == 2:
                return ""  # empty second map chunk
            return f"partial ok {map_n}"

        compressor = SessionCompressor(None)

        async def summarize(old_summary, to_compress):
            return await compressor._generate_summary(old_summary, to_compress)

        with patch(
            "src.everbot.core.agent.provider.oneshot_llm_provider"
        ) as mock_oneshot:
            mock_oneshot.return_value.call_llm = AsyncMock(side_effect=flaky_llm)
            result = await HistoryCompactionPolicy().ensure_within_budget(
                history, cfg, summarize=summarize
            )

        # Must not inject a partial summary as if success.
        for m in result.history:
            content = m.get("content")
            if isinstance(content, str):
                assert "partial ok" not in content
        assert result.outcome in (
            "window_trimmed",
            "over_budget_unavoidable",
            "kept_original",
        )
        assert validate_tool_pairing(result.history) == []