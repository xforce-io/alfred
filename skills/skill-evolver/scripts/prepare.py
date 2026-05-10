#!/usr/bin/env python3
"""skill-evolver prepare step.

Reads the target skill's current SKILL.md across the read priority chain,
generates a new userevolve-tagged version number, and emits a JSON payload
that the agent uses to drive its rewrite step.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone


_SUFFIX_RE = re.compile(r"-(?:user)?evolve-\d+$")


def _extract_base(version: str) -> str:
    """Strip any -evolve-<ts> or -userevolve-<ts> suffix to recover the base."""
    return _SUFFIX_RE.sub("", version)


def _new_version(current: str, *, ts: str | None = None) -> str:
    """Compute the new userevolve version from the current version string.

    Args:
        current: The existing SKILL.md frontmatter version, possibly with a
            prior -evolve- or -userevolve- suffix.
        ts: Optional timestamp override (YYYYMMDDHHMM) — supplied by tests
            for determinism. Defaults to UTC now.
    """
    base = _extract_base(current)
    if ts is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    return f"{base}-userevolve-{ts}"
