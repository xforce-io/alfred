#!/usr/bin/env python3
"""skill-evolver commit step.

Validates the rewritten SKILL.md content and publishes it as a new testing
version via VersionManager. Concurrent auto evolves serialize via skill_lock.
"""
from __future__ import annotations

import re


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_VERSION_LINE_RE = re.compile(r'^\s*version\s*:\s*["\']?([^"\'\n]+?)["\']?\s*$', re.MULTILINE)


def _validate_content(content: str, *, expected_version: str) -> None:
    """Raise ValueError unless content has well-formed frontmatter whose
    version matches expected_version exactly."""
    fm_match = _FRONTMATTER_RE.match(content)
    if not fm_match:
        raise ValueError("missing frontmatter (file must start with '---' block)")
    fm_body = fm_match.group(1)
    ver_match = _VERSION_LINE_RE.search(fm_body)
    if not ver_match:
        raise ValueError("frontmatter is missing the 'version:' field")
    actual = ver_match.group(1).strip()
    if actual != expected_version:
        raise ValueError(
            f"frontmatter version mismatch: file has '{actual}', expected '{expected_version}'"
        )
