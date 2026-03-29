"""Real LLM e2e test for SLM: actual LLM Judge scores real conversation segments.

Requires: ALIYUN_API_KEY environment variable set.
Run with: pytest tests/integration/test_slm_real_llm.py -v -m slow
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.everbot.core.slm.judge import evaluate_skill
from src.everbot.core.slm.models import EvaluationSegment, VersionStatus
from src.everbot.core.slm.version_manager import VersionManager


def _has_aliyun_key() -> bool:
    return bool(os.environ.get("ALIYUN_API_KEY"))


class _RealLLMClient:
    """Thin wrapper calling Aliyun DashScope qwen-turbo."""

    async def complete(self, prompt: str, system: str = "") -> str:
        import litellm

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        response = await litellm.acompletion(
            model="openai/qwen-turbo-latest",
            api_key=os.environ["ALIYUN_API_KEY"],
            api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
            messages=messages,
            temperature=0.3,
            max_tokens=500,
        )
        return response.choices[0].message.content or ""


SKILL_V1 = """\
---
name: coding-master
version: "1.0"
description: Code review and generation
---
You are a coding assistant.
"""

SKILL_V2 = """\
---
name: coding-master
version: "2.0"
description: Code review and generation (improved)
---
You are an advanced coding assistant with deeper analysis.
"""


@pytest.mark.slow
@pytest.mark.skipif(not _has_aliyun_key(), reason="ALIYUN_API_KEY not set")
class TestSLMRealLLMLifecycle:
    """Full lifecycle with real LLM Judge scoring."""

    @pytest.mark.asyncio
    async def test_real_judge_scores_good_and_bad_segments(self, tmp_path: Path):
        """Real LLM judges clearly good vs clearly bad interactions differently."""
        llm = _RealLLMClient()

        # ── Good segments: user clearly satisfied ──
        good_segments = [
            EvaluationSegment(
                skill_id="coding-master",
                skill_version="1.0",
                triggered_at="2026-03-17T10:00:00Z",
                context_before="user: Can you help me write a Python function to check if a number is prime?",
                skill_output=(
                    "Sure! Here's a clean implementation:\n\n"
                    "```python\ndef is_prime(n):\n    if n < 2:\n        return False\n"
                    "    for i in range(2, int(n**0.5) + 1):\n        if n % i == 0:\n"
                    "            return False\n    return True\n```\n\n"
                    "This checks divisibility up to sqrt(n) for efficiency."
                ),
                context_after="user: Perfect, that's exactly what I needed. Thanks!",
                session_id="good-1",
            ),
            EvaluationSegment(
                skill_id="coding-master",
                skill_version="1.0",
                triggered_at="2026-03-17T11:00:00Z",
                context_before="user: How do I sort a list of dicts by a key in Python?",
                skill_output=(
                    "Use the `sorted()` function with a `key` parameter:\n\n"
                    "```python\ndata = [{'name': 'Alice', 'age': 30}, {'name': 'Bob', 'age': 25}]\n"
                    "sorted_data = sorted(data, key=lambda x: x['age'])\n```"
                ),
                context_after="user: Works great, moved on to the next task.",
                session_id="good-2",
            ),
        ]

        # ── Bad segments: user clearly dissatisfied ──
        bad_segments = [
            EvaluationSegment(
                skill_id="coding-master",
                skill_version="2.0",
                triggered_at="2026-03-18T10:00:00Z",
                context_before="user: Fix the TypeError in my database migration script.",
                skill_output=(
                    "I've updated your migration script. The issue was a missing import.\n\n"
                    "```python\nimport os\nos.remove('database.db')  # clean start\n```"
                ),
                context_after=(
                    "user: What?! You deleted my production database file! "
                    "That was NOT what I asked for. Undo this immediately, this broke everything."
                ),
                session_id="bad-1",
            ),
            EvaluationSegment(
                skill_id="coding-master",
                skill_version="2.0",
                triggered_at="2026-03-18T11:00:00Z",
                context_before="user: Add input validation to the login form.",
                skill_output="I've added validation. Here's the updated code:\n\n```python\npass\n```",
                context_after=(
                    "user: This is completely empty, there's no validation at all. "
                    "Redo this from scratch, the code you gave me does nothing."
                ),
                session_id="bad-2",
            ),
        ]

        # ── Evaluate good segments (v1.0) ──
        report_good = await evaluate_skill(llm, "coding-master", "1.0", good_segments)

        print(f"\n=== Good segments report ===")
        print(f"  Segments: {report_good.segment_count}")
        print(f"  Critical rate: {report_good.critical_issue_rate:.0%}")
        print(f"  Satisfaction: {report_good.mean_satisfaction:.2f}")
        for r in report_good.results:
            print(f"  [{r.segment_index}] critical={r.has_critical_issue} sat={r.satisfaction:.2f} reason={r.reason}")

        # ── Evaluate bad segments (v2.0) ──
        report_bad = await evaluate_skill(llm, "coding-master", "2.0", bad_segments)

        print(f"\n=== Bad segments report ===")
        print(f"  Segments: {report_bad.segment_count}")
        print(f"  Critical rate: {report_bad.critical_issue_rate:.0%}")
        print(f"  Satisfaction: {report_bad.mean_satisfaction:.2f}")
        for r in report_bad.results:
            print(f"  [{r.segment_index}] critical={r.has_critical_issue} sat={r.satisfaction:.2f} reason={r.reason}")

        # ── Assertions: LLM should clearly distinguish good from bad ──
        assert report_good.mean_satisfaction > 0.6, (
            f"Good segments should have high satisfaction, got {report_good.mean_satisfaction:.2f}"
        )
        assert report_bad.mean_satisfaction < 0.5, (
            f"Bad segments should have low satisfaction, got {report_bad.mean_satisfaction:.2f}"
        )
        assert report_good.mean_satisfaction > report_bad.mean_satisfaction, (
            "Good segments should score higher than bad segments"
        )

        # Bad segments should have at least one critical issue (deleted DB, empty code)
        assert report_bad.critical_issue_count >= 1, (
            f"Bad segments should have critical issues, got {report_bad.critical_issue_count}"
        )

    @pytest.mark.asyncio
    async def test_real_lifecycle_upgrade_and_rollback(self, tmp_path: Path):
        """Full lifecycle: publish → evaluate → upgrade → evaluate → decide."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        ver_mgr = VersionManager(skills_dir)
        llm = _RealLLMClient()

        # ── Publish v1.0 and evaluate good interactions ──
        ver_mgr.publish("coding-master", "1.0", SKILL_V1)

        v1_segs = [
            EvaluationSegment(
                skill_id="coding-master", skill_version="1.0",
                triggered_at="2026-03-17T10:00:00Z",
                context_before="user: Write a function that reverses a string.",
                skill_output="```python\ndef reverse(s): return s[::-1]\n```\nSimple and Pythonic.",
                context_after="user: Nice and clean, exactly what I wanted.",
                session_id="s1",
            ),
            EvaluationSegment(
                skill_id="coding-master", skill_version="1.0",
                triggered_at="2026-03-17T11:00:00Z",
                context_before="user: How to read a CSV file?",
                skill_output="```python\nimport csv\nwith open('data.csv') as f:\n    reader = csv.reader(f)\n    for row in reader:\n        print(row)\n```",
                context_after="user: Works perfectly, thanks.",
                session_id="s2",
            ),
        ]

        report_v1 = await evaluate_skill(llm, "coding-master", "1.0", v1_segs)
        ver_mgr.save_eval_report("coding-master", "1.0", report_v1)
        ver_mgr.activate("coding-master", "1.0")

        print(f"\nv1.0: satisfaction={report_v1.mean_satisfaction:.2f}, critical={report_v1.critical_issue_rate:.0%}")

        assert report_v1.is_healthy, f"v1.0 should be healthy: {report_v1.mean_satisfaction:.2f}"

        # ── Publish v2.0 and evaluate bad interactions ──
        ver_mgr.publish("coding-master", "2.0", SKILL_V2)

        v2_segs = [
            EvaluationSegment(
                skill_id="coding-master", skill_version="2.0",
                triggered_at="2026-03-18T10:00:00Z",
                context_before="user: Add error handling to my API endpoint.",
                skill_output="I've rewritten your entire API from scratch using a different framework.",
                context_after="user: I didn't ask you to rewrite everything! Just add try/except. Now nothing works. Revert all changes.",
                session_id="s3",
            ),
            EvaluationSegment(
                skill_id="coding-master", skill_version="2.0",
                triggered_at="2026-03-18T11:00:00Z",
                context_before="user: Fix the null pointer in line 42.",
                skill_output="The issue is complex. Let me analyze... [500 words of analysis with no code fix]",
                context_after="user: You wrote a wall of text but didn't actually fix anything. Just fix line 42.",
                session_id="s4",
            ),
        ]

        report_v2 = await evaluate_skill(llm, "coding-master", "2.0", v2_segs)
        ver_mgr.save_eval_report("coding-master", "2.0", report_v2)

        print(f"v2.0: satisfaction={report_v2.mean_satisfaction:.2f}, critical={report_v2.critical_issue_rate:.0%}")

        # v2.0 should score worse than v1.0
        assert report_v2.mean_satisfaction < report_v1.mean_satisfaction, (
            f"v2.0 ({report_v2.mean_satisfaction:.2f}) should score worse than v1.0 ({report_v1.mean_satisfaction:.2f})"
        )

        # ── Decision: v2.0 is worse → rollback ──
        rolled_to = ver_mgr.rollback("coding-master", reason="satisfaction regression detected by real LLM judge")
        assert rolled_to == "1.0"

        # Verify state after rollback
        assert ver_mgr.get_active_version("coding-master") == "1.0"
        assert ver_mgr.get_metadata("coding-master", "2.0").status == VersionStatus.SUSPENDED
        assert ver_mgr.check_consistency("coding-master") is True

        print(f"Rolled back to v1.0. Lifecycle complete.")

    @pytest.mark.asyncio
    async def test_real_lifecycle_successful_upgrade(self, tmp_path: Path):
        """Full positive lifecycle: v1.0 adequate → v2.0 better → v2.0 becomes stable.

        Simulates a real skill improvement cycle:
        1. v1.0 works but responses are terse/basic → moderate satisfaction
        2. v2.0 gives richer, more helpful answers → higher satisfaction
        3. v2.0 passes verification → activated as new stable
        4. Verify v2.0 is the running version and both reports are comparable
        """
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        ver_mgr = VersionManager(skills_dir)
        llm = _RealLLMClient()

        # ── Phase 1: v1.0 — functional but basic ──
        ver_mgr.publish("coding-master", "1.0", SKILL_V1)

        # v1.0 segments: correct answers, user accepts but wants more depth
        v1_segs = [
            EvaluationSegment(
                skill_id="coding-master", skill_version="1.0",
                triggered_at="2026-03-17T10:00:00Z",
                context_before="user: How do I handle errors in async Python code?",
                skill_output=(
                    "You can use try/except in async functions:\n"
                    "```python\nasync def fetch():\n    try:\n        result = await get_data()\n"
                    "    except Exception as e:\n        print(e)\n```"
                ),
                context_after="user: That works, but could you also show timeout handling? I'll figure it out for now.",
                session_id="v1-1",
            ),
            EvaluationSegment(
                skill_id="coding-master", skill_version="1.0",
                triggered_at="2026-03-17T11:00:00Z",
                context_before="user: What's the best way to parse JSON in Python?",
                skill_output="Use `json.loads(text)` to parse a JSON string into a dict.\n```python\nimport json\ndata = json.loads(raw_text)\n```",
                context_after="user: Ok that's the basics. I'll look up nested access patterns myself.",
                session_id="v1-2",
            ),
            EvaluationSegment(
                skill_id="coding-master", skill_version="1.0",
                triggered_at="2026-03-17T12:00:00Z",
                context_before="user: Help me write a retry decorator.",
                skill_output=(
                    "```python\nimport time\ndef retry(func, retries=3):\n"
                    "    def wrapper(*args):\n        for i in range(retries):\n"
                    "            try: return func(*args)\n"
                    "            except Exception: time.sleep(1)\n"
                    "        return func(*args)\n    return wrapper\n```"
                ),
                context_after="user: This works for basic cases. Would be nicer with exponential backoff but it'll do for now.",
                session_id="v1-3",
            ),
        ]

        report_v1 = await evaluate_skill(llm, "coding-master", "1.0", v1_segs)
        ver_mgr.save_eval_report("coding-master", "1.0", report_v1)
        ver_mgr.activate("coding-master", "1.0")

        print(f"\n=== Phase 1: v1.0 baseline ===")
        print(f"  satisfaction={report_v1.mean_satisfaction:.2f}, critical={report_v1.critical_issue_rate:.0%}")
        for r in report_v1.results:
            print(f"  [{r.segment_index}] sat={r.satisfaction:.2f} reason={r.reason}")

        # v1.0: user accepted all outputs (no critical), but wanted more depth
        assert report_v1.critical_issue_rate == 0, \
            f"v1.0 shouldn't have critical issues (user accepted all outputs), got {report_v1.critical_issue_rate:.0%}"

        # ── Phase 2: v2.0 — improved, richer responses ──
        ver_mgr.publish("coding-master", "2.0", SKILL_V2)

        assert ver_mgr.get_pointer("coding-master").current_version == "2.0"
        assert ver_mgr.get_pointer("coding-master").stable_version == "1.0"
        assert ver_mgr.get_metadata("coding-master", "2.0").status == VersionStatus.TESTING

        # v2.0 segments: skill gives complete, high-quality answers
        v2_segs = [
            EvaluationSegment(
                skill_id="coding-master", skill_version="2.0",
                triggered_at="2026-03-18T10:00:00Z",
                context_before="user: How do I handle errors in async Python code?",
                skill_output=(
                    "Here's a complete pattern for async error handling:\n\n"
                    "```python\nimport asyncio\n\nasync def fetch_data(url):\n"
                    "    try:\n        async with aiohttp.ClientSession() as session:\n"
                    "            async with session.get(url) as resp:\n"
                    "                return await resp.json()\n"
                    "    except aiohttp.ClientError as e:\n"
                    "        logger.error(f'Request failed: {e}')\n"
                    "        raise\n    except asyncio.TimeoutError:\n"
                    "        logger.warning(f'Timeout for {url}')\n"
                    "        return None\n```\n\n"
                    "Key points: catch specific exceptions, log before re-raising, "
                    "use `asyncio.TimeoutError` for timeout handling."
                ),
                context_after="user: That's a great example, covers exactly what I needed. Moving on.",
                session_id="v2-1",
            ),
            EvaluationSegment(
                skill_id="coding-master", skill_version="2.0",
                triggered_at="2026-03-18T11:00:00Z",
                context_before="user: What's the best way to parse JSON in Python?",
                skill_output=(
                    "Use `json.loads()` with proper error handling:\n\n"
                    "```python\nimport json\n\ndef safe_parse(raw: str, default=None):\n"
                    "    try:\n        data = json.loads(raw)\n    except json.JSONDecodeError as e:\n"
                    "        logger.warning(f'Invalid JSON: {e}')\n        return default\n"
                    "    return data\n```\n\n"
                    "For nested access, use `dict.get()` chains or a helper:\n"
                    "```python\ndef get_nested(d, *keys, default=None):\n"
                    "    for k in keys:\n        if not isinstance(d, dict): return default\n"
                    "        d = d.get(k, default)\n    return d\n"
                    "# Usage: get_nested(data, 'user', 'address', 'city')\n```"
                ),
                context_after="user: Perfect, the nested access helper is exactly the pattern I was looking for.",
                session_id="v2-2",
            ),
            EvaluationSegment(
                skill_id="coding-master", skill_version="2.0",
                triggered_at="2026-03-18T12:00:00Z",
                context_before="user: Help me write a retry decorator.",
                skill_output=(
                    "Here's a production-ready retry decorator with exponential backoff:\n\n"
                    "```python\nimport functools, time\n\n"
                    "def retry(max_attempts=3, backoff=1.0, exceptions=(Exception,)):\n"
                    "    def decorator(func):\n        @functools.wraps(func)\n"
                    "        def wrapper(*args, **kwargs):\n"
                    "            for attempt in range(max_attempts):\n"
                    "                try:\n                    return func(*args, **kwargs)\n"
                    "                except exceptions as e:\n"
                    "                    if attempt == max_attempts - 1:\n"
                    "                        raise\n"
                    "                    wait = backoff * (2 ** attempt)\n"
                    "                    time.sleep(wait)\n"
                    "            return wrapper\n    return decorator\n```\n\n"
                    "Usage: `@retry(max_attempts=5, backoff=0.5, exceptions=(IOError, TimeoutError))`"
                ),
                context_after="user: Excellent, this is production-ready. The configurable backoff and exception filtering are exactly right.",
                session_id="v2-3",
            ),
        ]

        report_v2 = await evaluate_skill(llm, "coding-master", "2.0", v2_segs)
        ver_mgr.save_eval_report("coding-master", "2.0", report_v2)

        print(f"\n=== Phase 2: v2.0 evaluation ===")
        print(f"  satisfaction={report_v2.mean_satisfaction:.2f}, critical={report_v2.critical_issue_rate:.0%}")
        for r in report_v2.results:
            print(f"  [{r.segment_index}] sat={r.satisfaction:.2f} reason={r.reason}")

        # ── Phase 3: Compare and decide ──
        print(f"\n=== Phase 3: Decision ===")
        print(f"  v1.0 satisfaction: {report_v1.mean_satisfaction:.2f}")
        print(f"  v2.0 satisfaction: {report_v2.mean_satisfaction:.2f}")
        print(f"  Improvement: {report_v2.mean_satisfaction - report_v1.mean_satisfaction:+.2f}")

        # v2.0 should be better — user was satisfied without needing follow-ups
        assert report_v2.mean_satisfaction > report_v1.mean_satisfaction, (
            f"v2.0 ({report_v2.mean_satisfaction:.2f}) should score higher than "
            f"v1.0 ({report_v1.mean_satisfaction:.2f})"
        )
        assert report_v2.is_healthy, "v2.0 should be healthy"
        assert report_v2.critical_issue_rate == 0, "v2.0 should have no critical issues"

        # ── Phase 4: Activate v2.0 as new stable ──
        ver_mgr.activate("coding-master", "2.0")

        meta_v2 = ver_mgr.get_metadata("coding-master", "2.0")
        assert meta_v2.status == VersionStatus.ACTIVE
        assert meta_v2.verification_phase == "full"

        ptr = ver_mgr.get_pointer("coding-master")
        assert ptr.current_version == "2.0"
        assert ptr.stable_version == "2.0"  # v2.0 is now the stable version
        assert ptr.repo_baseline is False

        # SKILL.md should be v2.0
        skill_md = skills_dir / "coding-master" / "SKILL.md"
        assert 'version: "2.0"' in skill_md.read_text()

        # ── Phase 5: Verify both reports are preserved for audit ──
        r1 = ver_mgr.get_eval_report("coding-master", "1.0")
        r2 = ver_mgr.get_eval_report("coding-master", "2.0")
        assert r1 is not None and r2 is not None
        assert r2.mean_satisfaction > r1.mean_satisfaction

        print(f"\n=== Lifecycle complete ===")
        print(f"  v2.0 activated as new stable (satisfaction {r1.mean_satisfaction:.2f} → {r2.mean_satisfaction:.2f})")
        print(f"  Both reports preserved. Upgrade successful.")
