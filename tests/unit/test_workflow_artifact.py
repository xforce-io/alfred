"""Unit tests for workflow artifact extraction and injection."""

from src.everbot.core.workflow.artifact import (
    _truncate,
    build_artifact_injection,
    extract_artifact,
)


class TestExtractArtifact:
    def test_extract_with_tag(self):
        output = "thinking...\n<phase_artifact>my plan here</phase_artifact>\ndone"
        assert extract_artifact("plan", output, "") == "my plan here"

    def test_extract_with_tag_multiline(self):
        output = "<phase_artifact>\n## Plan\n- step 1\n- step 2\n</phase_artifact>"
        result = extract_artifact("plan", output, "")
        assert "## Plan" in result
        assert "step 1" in result

    def test_extract_with_tag_strips_whitespace(self):
        output = "<phase_artifact>  artifact content  </phase_artifact>"
        assert extract_artifact("test", output, "") == "artifact content"

    def test_fallback_to_last_assistant_text(self):
        result = extract_artifact("research", "no tag output", "fallback text")
        assert result == "fallback text"

    def test_fallback_truncated(self):
        long_text = "x" * 5000
        result = extract_artifact("test", "", long_text)
        assert len(result) < 5000
        assert "truncated" in result

    def test_empty_inputs(self):
        assert extract_artifact("test", "", "") == ""

    def test_tag_in_llm_output_preferred_over_assistant(self):
        output = "blah <phase_artifact>from output</phase_artifact>"
        result = extract_artifact("test", output, "from assistant")
        assert result == "from output"

    def test_multiple_tags_first_wins(self):
        output = "<phase_artifact>first</phase_artifact> <phase_artifact>second</phase_artifact>"
        assert extract_artifact("test", output, "") == "first"


class TestBuildArtifactInjection:
    def test_basic_injection(self):
        artifacts = {"research": "found bug in module X"}
        result = build_artifact_injection(artifacts, ["research"])
        assert "research" in result
        assert "found bug in module X" in result

    def test_multiple_artifacts(self):
        artifacts = {
            "research": "found bug",
            "plan": "fix by refactoring Y",
        }
        result = build_artifact_injection(artifacts, ["research", "plan"])
        assert "research" in result
        assert "plan" in result
        assert "found bug" in result
        assert "fix by refactoring" in result

    def test_empty_input_artifacts(self):
        assert build_artifact_injection({"a": "b"}, []) == ""

    def test_missing_artifact_reference(self):
        """Missing artifact produces empty result with warning."""
        result = build_artifact_injection({}, ["nonexistent"])
        assert result == ""

    def test_truncation(self):
        """Long artifacts are truncated per-artifact."""
        artifacts = {"big": "x" * 10000}
        result = build_artifact_injection(artifacts, ["big"])
        assert "truncated" in result
        assert len(result) < 10000

    def test_partial_missing(self):
        """Mix of existing and missing artifacts."""
        artifacts = {"research": "found X"}
        result = build_artifact_injection(artifacts, ["research", "missing"])
        assert "research" in result
        assert "found X" in result


class TestTruncate:
    def test_short_text_unchanged(self):
        assert _truncate("hello", 100) == "hello"

    def test_long_text_truncated(self):
        result = _truncate("x" * 200, 100)
        assert len(result) < 200
        assert "truncated" in result

    def test_empty_text(self):
        assert _truncate("", 100) == ""

    def test_exact_limit(self):
        text = "x" * 100
        assert _truncate(text, 100) == text
