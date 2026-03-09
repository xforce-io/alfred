"""Shared utilities for reflection skills."""

import json
import re


def parse_json_response(response: str) -> dict:
    """Extract JSON from LLM response (handles markdown code blocks)."""
    match = re.search(r"```(?:json)?\s*\n(.*?)\n\s*```", response, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    return json.loads(response.strip())
