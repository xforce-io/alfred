"""LLM Judge — score Evaluation Segments for quality and satisfaction."""

from __future__ import annotations

import json
import logging
from typing import List, Protocol

from .models import EvalReport, EvaluationSegment, JudgeResult

logger = logging.getLogger(__name__)

_JUDGE_SYSTEM = "You are an evaluation judge. Analyze the skill invocation and output valid JSON only."

_JUDGE_PROMPT = """\
Evaluate this skill invocation based on the user's reaction after the skill produced its output.

## Context Before (1 turn before skill invocation)
{context_before}

## Skill Output
{skill_output}

## Context After (1 turn after — user's reaction)
{context_after}

## Evaluation Criteria
1. **has_critical_issue**: true if the skill output caused an obvious error, broke something, \
or led the user to redo/reject the output entirely.
2. **satisfaction**: float 0.0-1.0 based on user's reaction:
   - 1.0: user accepted and continued smoothly
   - 0.7-0.9: user accepted with minor corrections
   - 0.4-0.6: user needed significant corrections
   - 0.1-0.3: user was clearly dissatisfied or had to redo
   - 0.0: skill output was harmful or completely wrong

Output format:
```json
{{"has_critical_issue": false, "satisfaction": 0.8, "reason": "brief explanation"}}
```"""


class LLMClient(Protocol):
    """Minimal LLM client interface."""

    async def complete(self, prompt: str, system: str = "") -> str: ...


async def judge_segment(llm: LLMClient, segment: EvaluationSegment) -> JudgeResult:
    """Score a single Evaluation Segment using LLM Judge."""
    prompt = _JUDGE_PROMPT.format(
        context_before=segment.context_before or "(empty)",
        skill_output=segment.skill_output or "(empty)",
        context_after=segment.context_after or "(empty)",
    )
    response = await llm.complete(prompt, system=_JUDGE_SYSTEM)
    data = _parse_judge_response(response)
    return JudgeResult(
        segment_index=0,  # caller should set the real index
        has_critical_issue=bool(data.get("has_critical_issue", False)),
        satisfaction=max(0.0, min(1.0, float(data.get("satisfaction", 0.0)))),
        reason=str(data.get("reason", "")),
    )


async def judge_segments(
    llm: LLMClient,
    segments: List[EvaluationSegment],
) -> List[JudgeResult]:
    """Score multiple segments sequentially.

    Returns results in the same order as input segments, with segment_index set.
    """
    results: List[JudgeResult] = []
    for i, segment in enumerate(segments):
        try:
            result = await judge_segment(llm, segment)
            result.segment_index = i
            results.append(result)
        except Exception as e:
            logger.warning("Failed to judge segment %d: %s", i, e)
            # Default to neutral score on failure to avoid blocking evaluation
            results.append(JudgeResult(
                segment_index=i,
                has_critical_issue=False,
                satisfaction=0.5,
                reason=f"Judge error: {e}",
            ))
    return results


async def evaluate_skill(
    llm: LLMClient,
    skill_id: str,
    skill_version: str,
    segments: List[EvaluationSegment],
) -> EvalReport:
    """Run full evaluation: judge all segments and build report."""
    results = await judge_segments(llm, segments)
    return EvalReport.build(skill_id, skill_version, results)


def _parse_judge_response(response: str) -> dict:
    """Extract JSON from LLM response (handles markdown code blocks)."""
    import re

    # Try markdown code block first (flexible: newline before closing ``` optional)
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", response, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Try bare JSON parse
    try:
        return json.loads(response.strip())
    except json.JSONDecodeError:
        pass

    # Fallback: extract first JSON object from response text
    start = response.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(response)):
            if response[i] == "{":
                depth += 1
            elif response[i] == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(response[start : i + 1])

    raise json.JSONDecodeError("No JSON found in response", response, 0)
