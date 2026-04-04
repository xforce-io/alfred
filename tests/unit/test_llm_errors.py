"""Tests for LLM error classification."""

from src.everbot.core.jobs.llm_errors import LLMTransientError, LLMConfigError


class TestLLMErrors:
    def test_transient_error_is_exception(self):
        err = LLMTransientError("connection refused")
        assert isinstance(err, Exception)
        assert str(err) == "connection refused"

    def test_config_error_is_exception(self):
        err = LLMConfigError("model not found")
        assert isinstance(err, Exception)
        assert str(err) == "model not found"

    def test_transient_and_config_are_distinct(self):
        """Framework must be able to catch them separately."""
        transient = LLMTransientError("timeout")
        config = LLMConfigError("bad key")
        assert not isinstance(transient, LLMConfigError)
        assert not isinstance(config, LLMTransientError)
