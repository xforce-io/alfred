"""Unit tests for workflow PhaseContextManager."""

from src.everbot.core.workflow.context_manager import PhaseContextManager
from src.everbot.core.workflow.models import PhaseConfig


# ---------------------------------------------------------------------------
# Fake agent for testing context operations
# ---------------------------------------------------------------------------

class _FakeContext:
    def __init__(self):
        self._vars = {}

    def set_variable(self, key, value):
        self._vars[key] = value

    def get_var_value(self, key):
        return self._vars.get(key)


class _FakeExecutor:
    def __init__(self):
        self.context = _FakeContext()


class _FakeAgent:
    def __init__(self):
        self.executor = _FakeExecutor()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestClearHistory:
    def test_clears_history(self):
        agent = _FakeAgent()
        agent.executor.context.set_variable("_history", [{"content": "old"}])
        mgr = PhaseContextManager(agent)
        mgr.clear_history()
        assert agent.executor.context.get_var_value("_history") == []


class TestEstimateHistoryTokens:
    def test_empty_history(self):
        agent = _FakeAgent()
        mgr = PhaseContextManager(agent)
        assert mgr.estimate_history_tokens() == 0

    def test_string_content(self):
        agent = _FakeAgent()
        agent.executor.context.set_variable("_history", [
            {"content": "x" * 300},  # 300 chars ≈ 100 tokens
        ])
        mgr = PhaseContextManager(agent)
        assert mgr.estimate_history_tokens() == 100

    def test_list_content(self):
        agent = _FakeAgent()
        agent.executor.context.set_variable("_history", [
            {"content": [{"text": "hello"}]},
        ])
        mgr = PhaseContextManager(agent)
        assert mgr.estimate_history_tokens() > 0

    def test_mixed_history(self):
        agent = _FakeAgent()
        agent.executor.context.set_variable("_history", [
            {"content": "abc"},
            "plain string entry",
            {"content": [{"text": "def"}]},
        ])
        mgr = PhaseContextManager(agent)
        assert mgr.estimate_history_tokens() > 0


class TestDetermineContextMode:
    def test_iteration_1_is_clean(self):
        agent = _FakeAgent()
        mgr = PhaseContextManager(agent)
        assert mgr.determine_context_mode(1) == "clean"

    def test_iteration_2_small_history_inherit(self):
        agent = _FakeAgent()
        # Small history: 300 chars ≈ 100 tokens, well under 32K
        agent.executor.context.set_variable("_history", [
            {"content": "x" * 300},
        ])
        mgr = PhaseContextManager(agent)
        assert mgr.determine_context_mode(2) == "inherit"

    def test_iteration_2_large_history_clean(self):
        agent = _FakeAgent()
        # Large history: 100K chars ≈ 33K tokens, over 32K
        agent.executor.context.set_variable("_history", [
            {"content": "x" * 100_000},
        ])
        mgr = PhaseContextManager(agent)
        assert mgr.determine_context_mode(2) == "clean"

    def test_iteration_3_plus_always_clean(self):
        agent = _FakeAgent()
        mgr = PhaseContextManager(agent)
        for i in range(3, 10):
            assert mgr.determine_context_mode(i) == "clean"


class TestPreparePhaseContext:
    def test_clean_mode_clears_history(self):
        agent = _FakeAgent()
        agent.executor.context.set_variable("_history", [{"content": "old"}])
        mgr = PhaseContextManager(agent)
        msg = mgr.prepare_phase_context(
            artifact_injection="",
            retry_context="",
            failure_history=[],
            context_mode="clean",
        )
        assert agent.executor.context.get_var_value("_history") == []
        assert "请开始" in msg

    def test_inherit_mode_preserves_history(self):
        agent = _FakeAgent()
        agent.executor.context.set_variable("_history", [{"content": "old"}])
        mgr = PhaseContextManager(agent)
        mgr.prepare_phase_context(
            artifact_injection="",
            retry_context="",
            failure_history=[],
            context_mode="inherit",
        )
        assert len(agent.executor.context.get_var_value("_history")) == 1

    def test_artifact_injection_included(self):
        agent = _FakeAgent()
        mgr = PhaseContextManager(agent)
        msg = mgr.prepare_phase_context(
            artifact_injection="## research 阶段产出\n\nfound bug in X",
            retry_context="",
            failure_history=[],
            context_mode="clean",
        )
        assert "research" in msg
        assert "found bug in X" in msg

    def test_retry_context_included(self):
        agent = _FakeAgent()
        mgr = PhaseContextManager(agent)
        msg = mgr.prepare_phase_context(
            artifact_injection="",
            retry_context="previous attempt failed: test error",
            failure_history=[],
            context_mode="clean",
        )
        assert "重试上下文" in msg
        assert "previous attempt failed" in msg

    def test_failure_history_included_on_clean(self):
        agent = _FakeAgent()
        mgr = PhaseContextManager(agent)
        msg = mgr.prepare_phase_context(
            artifact_injection="",
            retry_context="",
            failure_history=["fail1", "fail2"],
            context_mode="clean",
        )
        assert "历史失败记录" in msg
        assert "fail1" in msg
        assert "fail2" in msg

    def test_failure_history_not_included_on_inherit(self):
        """On inherit mode, failure_history should NOT be injected (it's in context)."""
        agent = _FakeAgent()
        mgr = PhaseContextManager(agent)
        msg = mgr.prepare_phase_context(
            artifact_injection="",
            retry_context="",
            failure_history=["fail1"],
            context_mode="inherit",
        )
        assert "历史失败记录" not in msg


class TestBuildPhaseSystemPrompt:
    def test_basic_prompt(self):
        agent = _FakeAgent()
        mgr = PhaseContextManager(agent)
        phase = PhaseConfig(name="research", instruction_ref="sop.md")
        prompt = mgr.build_phase_system_prompt(
            "You are a helpful agent.",
            phase,
            instruction_content="Research the codebase.",
        )
        assert "helpful agent" in prompt
        assert "Research the codebase" in prompt
        assert "phase_artifact" in prompt  # artifact protocol

    def test_tool_restriction(self):
        agent = _FakeAgent()
        mgr = PhaseContextManager(agent)
        phase = PhaseConfig(
            name="research",
            instruction_ref="sop.md",
            allowed_tools=["_bash", "_read_file"],
        )
        prompt = mgr.build_phase_system_prompt("base", phase)
        assert "_bash" in prompt
        assert "_read_file" in prompt
        assert "工具限制" in prompt

    def test_no_tool_restriction(self):
        agent = _FakeAgent()
        mgr = PhaseContextManager(agent)
        phase = PhaseConfig(name="impl", instruction_ref="sop.md")
        prompt = mgr.build_phase_system_prompt("base", phase)
        assert "工具限制" not in prompt

    def test_verification_cmd_no_artifact_protocol(self):
        """Cmd phases don't get artifact output protocol."""
        from src.everbot.core.workflow.models import VerificationCmdConfig
        agent = _FakeAgent()
        mgr = PhaseContextManager(agent)
        phase = PhaseConfig(
            name="verify",
            verification_cmd=VerificationCmdConfig(cmd="pytest"),
        )
        prompt = mgr.build_phase_system_prompt("base", phase)
        assert "phase_artifact" not in prompt

    def test_verify_protocol_prompt(self):
        agent = _FakeAgent()
        mgr = PhaseContextManager(agent)
        phase = PhaseConfig(
            name="check",
            instruction_ref="check.md",
            verify_protocol="structured_tag",
        )
        prompt = mgr.build_phase_system_prompt(
            "base", phase, is_verify=True
        )
        assert "verify_result" in prompt
        assert "PASS" in prompt
        assert "FAIL" in prompt

    def test_verify_protocol_only_when_is_verify(self):
        agent = _FakeAgent()
        mgr = PhaseContextManager(agent)
        phase = PhaseConfig(
            name="check",
            instruction_ref="check.md",
            verify_protocol="structured_tag",
        )
        prompt = mgr.build_phase_system_prompt(
            "base", phase, is_verify=False
        )
        assert "verify_result" not in prompt
