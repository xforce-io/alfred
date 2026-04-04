"""LLM Judge — score Evaluation Segments for quality and satisfaction."""

from __future__ import annotations

import json
import logging
from typing import List, Protocol

from .models import EvalReport, EvaluationSegment, JudgeResult

logger = logging.getLogger(__name__)

_JUDGE_SYSTEM = "You are an evaluation judge. Analyze skill invocations and output valid JSON only."

_BATCH_JUDGE_PROMPT = """\
Evaluate all invocations of a skill based on user reactions after each invocation.

{segments_block}

## Evaluation Criteria (apply to each segment independently)
1. **has_critical_issue**: true if the skill output caused an obvious error, broke something, \
or led the user to redo/reject the output entirely.
2. **satisfaction**: float 0.0-1.0 based on user's reaction:
   - 1.0: user accepted and continued smoothly
   - 0.7-0.9: user accepted with minor corrections
   - 0.4-0.6: user needed significant corrections
   - 0.1-0.3: user was clearly dissatisfied or had to redo
   - 0.0: skill output was harmful or completely wrong

Output a JSON array with one object per segment, in order:
```json
[
  {{"has_critical_issue": false, "satisfaction": 0.8, "reason": "brief explanation"}},
  ...
]
```"""


class LLMClient(Protocol):
    """Minimal LLM client interface."""

    async def complete(self, prompt: str, system: str = "") -> str: ...


def _build_segments_block(segments: List[EvaluationSegment]) -> str:
    """Format all segments into a numbered block for the batch prompt."""
    parts = []
    for i, seg in enumerate(segments):
        parts.append(
            f"### Segment {i}\n"
            f"**Context Before:** {seg.context_before or '(empty)'}\n"
            f"**Skill Output:** {seg.skill_output or '(empty)'}\n"
            f"**Context After:** {seg.context_after or '(empty)'}"
        )
    return "\n\n".join(parts)


async def judge_segments(
    llm: LLMClient,
    segments: List[EvaluationSegment],
) -> List[JudgeResult]:
    """Score all segments in a single LLM call.

    Returns results in the same order as input segments, with segment_index set.
    """
    if not segments:
        return []

    from ..jobs.llm_errors import LLMTransientError, LLMConfigError

    prompt = _BATCH_JUDGE_PROMPT.format(
        segments_block=_build_segments_block(segments),
    )

    try:
        response = await llm.complete(prompt, system=_JUDGE_SYSTEM)
    except (LLMTransientError, LLMConfigError):
        raise
    except Exception as e:
        logger.warning("Batch judge failed: %s", e)
        return [
            JudgeResult(
                segment_index=i,
                has_critical_issue=False,
                satisfaction=0.5,
                reason=f"Judge error: {e}",
            )
            for i in range(len(segments))
        ]

    try:
        items = _parse_batch_response(response, len(segments))
    except Exception as e:
        logger.warning("Batch judge parse failed: %s", e)
        return [
            JudgeResult(
                segment_index=i,
                has_critical_issue=False,
                satisfaction=0.5,
                reason=f"Parse error: {e}",
            )
            for i in range(len(segments))
        ]

    results: List[JudgeResult] = []
    for i, data in enumerate(items):
        results.append(JudgeResult(
            segment_index=i,
            has_critical_issue=bool(data.get("has_critical_issue", False)),
            satisfaction=max(0.0, min(1.0, float(data.get("satisfaction", 0.0)))),
            reason=str(data.get("reason", "")),
        ))
    return results


async def evaluate_skill(
    llm: LLMClient,
    skill_id: str,
    skill_version: str,
    segments: List[EvaluationSegment],
) -> EvalReport:
    """Run full evaluation: judge all segments in one LLM call and build report."""
    results = await judge_segments(llm, segments)
    return EvalReport.build(skill_id, skill_version, results)


def _parse_batch_response(response: str, expected_count: int) -> List[dict]:
    """Extract JSON array from LLM response, with fallback for count mismatch."""
    import re

    match = re.search(r"```(?:json)?\s*\n(.*?)\n\s*```", response, re.DOTALL)
    raw = match.group(1) if match else response.strip()
    parsed = json.loads(raw)

    # Handle single-object response (LLM forgot the array wrapper)
    if isinstance(parsed, dict):
        parsed = [parsed]

    if not isinstance(parsed, list):
        raise ValueError(f"Expected JSON array, got {type(parsed).__name__}")

    # Pad or truncate to match expected segment count
    while len(parsed) < expected_count:
        parsed.append({"has_critical_issue": False, "satisfaction": 0.5, "reason": "missing from judge response"})

    return parsed[:expected_count]
