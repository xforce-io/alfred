"""Typed exceptions for LLM call failures in skill jobs.

Used by _SkillLLMClient to classify raw exceptions, and by
_invoke_job to decide whether to advance watermark.
"""


class LLMTransientError(Exception):
    """LLM temporarily unavailable: connection failure, timeout, rate limit, 5xx.

    Safe to retry on next scheduled run.
    """


class LLMConfigError(Exception):
    """LLM configuration problem: model not found, auth failure, missing dependency.

    Requires manual intervention to fix.
    """
