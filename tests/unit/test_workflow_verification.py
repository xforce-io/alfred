"""Unit tests for workflow verification module."""


import pytest

from src.everbot.core.workflow.models import VerificationCmdConfig
from src.everbot.core.workflow.verification import (
    extract_verify_result,
    run_verification_cmd,
)


# ---------------------------------------------------------------------------
# extract_verify_result
# ---------------------------------------------------------------------------

class TestExtractVerifyResult:
    def test_pass(self):
        assert extract_verify_result(
            "<verify_result>PASS</verify_result>", "structured_tag"
        ) is True

    def test_pass_case_insensitive(self):
        assert extract_verify_result(
            "<verify_result>pass</verify_result>", "structured_tag"
        ) is True

    def test_pass_with_whitespace(self):
        assert extract_verify_result(
            "<verify_result>  PASS  </verify_result>", "structured_tag"
        ) is True

    def test_fail(self):
        assert extract_verify_result(
            "<verify_result>FAIL: tests broke</verify_result>", "structured_tag"
        ) is False

    def test_fail_no_reason(self):
        assert extract_verify_result(
            "<verify_result>FAIL</verify_result>", "structured_tag"
        ) is False

    def test_no_tag_conservative(self):
        """Missing tag → conservative False."""
        assert extract_verify_result("no tag here", "structured_tag") is False

    def test_wrong_protocol(self):
        assert extract_verify_result(
            "<verify_result>PASS</verify_result>", "unknown"
        ) is False

    def test_none_protocol(self):
        assert extract_verify_result(
            "<verify_result>PASS</verify_result>", None
        ) is False

    def test_tag_in_surrounding_text(self):
        text = "Some analysis...\n<verify_result>PASS</verify_result>\nDone."
        assert extract_verify_result(text, "structured_tag") is True

    def test_multiline_content(self):
        text = "<verify_result>\nFAIL: line1\nline2\n</verify_result>"
        assert extract_verify_result(text, "structured_tag") is False


# ---------------------------------------------------------------------------
# run_verification_cmd
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_verification_cmd_success():
    config = VerificationCmdConfig(cmd="echo 'all tests passed'")
    result = await run_verification_cmd(
        config, skill_dir="/tmp", project_dir="/tmp", session_id="wf_test"
    )
    assert result.exit_code == 0
    assert "all tests passed" in result.output


@pytest.mark.asyncio
async def test_run_verification_cmd_failure():
    config = VerificationCmdConfig(cmd="exit 1")
    result = await run_verification_cmd(
        config, skill_dir="/tmp", project_dir="/tmp", session_id="wf_test"
    )
    assert result.exit_code == 1


@pytest.mark.asyncio
async def test_run_verification_cmd_timeout():
    config = VerificationCmdConfig(cmd="sleep 10", timeout_seconds=1)
    result = await run_verification_cmd(
        config, skill_dir="/tmp", project_dir="/tmp", session_id="wf_test"
    )
    assert result.exit_code == 1
    assert "timed out" in result.output.lower()


@pytest.mark.asyncio
async def test_run_verification_cmd_env_vars():
    """Environment variables SKILL_DIR etc. are injected."""
    config = VerificationCmdConfig(cmd="echo $SKILL_DIR $PROJECT_DIR $WORKFLOW_SESSION_ID")
    result = await run_verification_cmd(
        config,
        skill_dir="/tmp",
        project_dir="/tmp",
        session_id="wf_abc123",
    )
    assert result.exit_code == 0
    assert "/tmp" in result.output
    assert "wf_abc123" in result.output


@pytest.mark.asyncio
async def test_run_verification_cmd_custom_env():
    config = VerificationCmdConfig(cmd="echo $MY_VAR", env={"MY_VAR": "hello"})
    result = await run_verification_cmd(
        config, skill_dir="/tmp", project_dir="/tmp", session_id="wf_test"
    )
    assert "hello" in result.output


@pytest.mark.asyncio
async def test_run_verification_cmd_output_truncation():
    """Output exceeding 4000 chars is truncated."""
    config = VerificationCmdConfig(cmd="python3 -c \"print('x' * 5000)\"")
    result = await run_verification_cmd(
        config, skill_dir="/tmp", project_dir="/tmp", session_id="wf_test"
    )
    assert result.exit_code == 0
    assert "truncated" in result.output
    assert len(result.output) < 5100


@pytest.mark.asyncio
async def test_run_verification_cmd_stderr_captured():
    """stderr is captured along with stdout."""
    config = VerificationCmdConfig(cmd="echo 'err msg' >&2")
    result = await run_verification_cmd(
        config, skill_dir="/tmp", project_dir="/tmp", session_id="wf_test"
    )
    assert "err msg" in result.output
