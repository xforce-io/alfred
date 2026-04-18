# Skill Evolve Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the evaluate→evolve loop so unhealthy skills are automatically rolled back, improved by LLM, and re-verified.

**Architecture:** Extend `skill_evaluate.py` with post-evaluation logic: unhealthy → rollback → LLM generates improved SKILL.md → publish as testing. Next eval cycle: healthy testing → activate. Consecutive failures → suspend. No new files.

**Tech Stack:** Python, asyncio, existing SLM infrastructure (VersionManager, SegmentLogger, judge)

---

### Task 1: Extend CurrentPointer with `consecutive_evolve_count`

**Files:**
- Modify: `src/everbot/core/slm/models.py:204-228`
- Test: `tests/unit/test_slm_models.py`

- [ ] **Step 1: Write the failing test**

```python
# In tests/unit/test_slm_models.py, add to the end:

class TestCurrentPointerEvolveCount:
    def test_default_evolve_count(self):
        pointer = CurrentPointer(current_version="1.0", stable_version="0.9")
        assert pointer.consecutive_evolve_count == 0

    def test_roundtrip_with_evolve_count(self):
        pointer = CurrentPointer(
            current_version="1.0",
            stable_version="0.9",
            consecutive_evolve_count=2,
        )
        json_str = pointer.to_json()
        restored = CurrentPointer.from_json(json_str)
        assert restored.consecutive_evolve_count == 2

    def test_backward_compat_missing_field(self):
        """Old current.json without consecutive_evolve_count loads as 0."""
        data = {"current_version": "1.0", "stable_version": "0.9", "repo_baseline": False}
        pointer = CurrentPointer.from_dict(data)
        assert pointer.consecutive_evolve_count == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_slm_models.py::TestCurrentPointerEvolveCount -v`
Expected: FAIL — `TypeError: unexpected keyword argument 'consecutive_evolve_count'`

- [ ] **Step 3: Add the field to CurrentPointer**

In `src/everbot/core/slm/models.py`, modify `CurrentPointer`:

```python
@dataclass
class CurrentPointer:
    """Points to the current and stable versions of a skill."""

    current_version: str
    stable_version: str
    repo_baseline: bool = True  # if True, stable = repo's original SKILL.md
    consecutive_evolve_count: int = 0  # cleared on activate

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CurrentPointer:
        return cls(
            current_version=str(data.get("current_version", "")),
            stable_version=str(data.get("stable_version", "")),
            repo_baseline=bool(data.get("repo_baseline", True)),
            consecutive_evolve_count=int(data.get("consecutive_evolve_count", 0)),
        )

    @classmethod
    def from_json(cls, text: str) -> CurrentPointer:
        return cls.from_dict(json.loads(text))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_slm_models.py::TestCurrentPointerEvolveCount -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/everbot/core/slm/models.py tests/unit/test_slm_models.py
git commit -m "feat(slm): add consecutive_evolve_count to CurrentPointer"
```

---

### Task 2: VersionManager.activate() clears evolve count

**Files:**
- Modify: `src/everbot/core/slm/version_manager.py:215-232`
- Test: `tests/unit/test_slm_version_manager.py`

- [ ] **Step 1: Write the failing test**

```python
# In tests/unit/test_slm_version_manager.py, add:

class TestActivateClearsEvolveCount:
    def test_activate_resets_consecutive_evolve_count(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        mgr = VersionManager(skills_dir)

        # Publish v1, then v2
        mgr.publish("test-skill", "1.0", SKILL_CONTENT_V1)
        mgr.publish("test-skill", "2.0", SKILL_CONTENT_V2)

        # Simulate evolve count
        pointer = mgr.get_pointer("test-skill")
        pointer.consecutive_evolve_count = 2
        mgr._current_json("test-skill").write_text(pointer.to_json(), encoding="utf-8")

        # Activate should clear it
        mgr.activate("test-skill", "2.0")

        pointer = mgr.get_pointer("test-skill")
        assert pointer.consecutive_evolve_count == 0
        assert pointer.stable_version == "2.0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_slm_version_manager.py::TestActivateClearsEvolveCount -v`
Expected: FAIL — `assert 2 == 0`

- [ ] **Step 3: Add evolve count reset to activate()**

In `src/everbot/core/slm/version_manager.py`, modify `activate()`:

```python
    def activate(self, skill_id: str, version: str) -> None:
        """Mark a version as active (passed all verification phases)."""
        meta = self.get_metadata(skill_id, version)
        if not meta:
            raise ValueError(f"Version {version} not found for {skill_id}")
        meta.status = VersionStatus.ACTIVE
        meta.verification_phase = "full"
        ver_dir = self._version_dir(skill_id, version)
        (ver_dir / "metadata.json").write_text(meta.to_json(), encoding="utf-8")

        # Update pointer: this version becomes stable, clear evolve count
        pointer = self.get_pointer(skill_id)
        if pointer:
            pointer.stable_version = version
            pointer.repo_baseline = False
            pointer.consecutive_evolve_count = 0
            self._current_json(skill_id).write_text(pointer.to_json(), encoding="utf-8")

        logger.info("Activated %s v%s", skill_id, version)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_slm_version_manager.py::TestActivateClearsEvolveCount -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/everbot/core/slm/version_manager.py tests/unit/test_slm_version_manager.py
git commit -m "feat(slm): activate() clears consecutive_evolve_count"
```

---

### Task 3: Add evolve logic to skill_evaluate

**Files:**
- Modify: `src/everbot/core/jobs/skill_evaluate.py`
- Test: `tests/unit/test_skill_evaluate_job.py`

This is the core task. It adds three behaviors to `_evaluate_one`:
1. Testing + healthy → activate
2. Unhealthy → rollback + evolve
3. Evolve count exceeded → suspend

- [ ] **Step 1: Write test for testing→activate path**

```python
# In tests/unit/test_skill_evaluate_job.py, add:

from src.everbot.core.slm.models import (
    CurrentPointer,
    EvalReport,
    EvaluationSegment,
    JudgeResult,
    VersionMetadata,
    VersionStatus,
)


def _make_healthy_report(skill_id: str, version: str, n: int = 3) -> EvalReport:
    return EvalReport(
        skill_id=skill_id,
        skill_version=version,
        evaluated_at="2026-04-12T00:00:00+00:00",
        segment_count=n,
        critical_issue_count=0,
        critical_issue_rate=0.0,
        mean_satisfaction=0.85,
        results=[
            JudgeResult(segment_index=i, has_critical_issue=False, satisfaction=0.85, reason="ok")
            for i in range(n)
        ],
    )


def _make_unhealthy_report(skill_id: str, version: str, n: int = 4) -> EvalReport:
    results = [
        JudgeResult(segment_index=i, has_critical_issue=(i % 2 == 0), satisfaction=0.3, reason="bad output")
        for i in range(n)
    ]
    critical = sum(1 for r in results if r.has_critical_issue)
    return EvalReport(
        skill_id=skill_id,
        skill_version=version,
        evaluated_at="2026-04-12T00:00:00+00:00",
        segment_count=n,
        critical_issue_count=critical,
        critical_issue_rate=critical / n,
        mean_satisfaction=0.3,
        results=results,
    )


def _setup_skill_with_version(
    skills_dir, logs_dir, eval_dir, skill_id, version, status=VersionStatus.TESTING
):
    """Create a skill with a specific version and status."""
    ver_mgr = VersionManager(skills_dir, eval_base_dir=eval_dir)
    content = f"---\nname: {skill_id}\nversion: \"{version}\"\n---\nSkill content"
    ver_mgr.publish(skill_id, version, content)
    # Set the desired status
    meta = ver_mgr.get_metadata(skill_id, version)
    meta.status = status
    ver_dir = eval_dir / skill_id / "versions" / f"v{version}"
    (ver_dir / "metadata.json").write_text(meta.to_json(), encoding="utf-8")
    # Write segments
    seg_logger = SegmentLogger(logs_dir)
    for i in range(3):
        seg_logger.append(EvaluationSegment(
            skill_id=skill_id,
            skill_version=version,
            triggered_at=f"2026-04-12T0{i}:00:00+00:00",
            context_before="user: do something",
            skill_output=f"output {i}",
            context_after="user: ok",
            session_id=f"sess-{i}",
        ))
    return ver_mgr, seg_logger


@pytest.mark.asyncio
async def test_testing_healthy_activates(tmp_path: Path):
    """Testing version + healthy report → activate + evolve_count cleared."""
    skills_dir = tmp_path / "skills"
    logs_dir = tmp_path / "skill_logs"
    eval_dir = tmp_path / "skill_eval"
    skills_dir.mkdir()

    ver_mgr, seg_logger = _setup_skill_with_version(
        skills_dir, logs_dir, eval_dir, "my-skill", "1.0-evolve-202604", VersionStatus.TESTING,
    )
    # Set evolve count to non-zero
    pointer = ver_mgr.get_pointer("my-skill")
    pointer.consecutive_evolve_count = 1
    ver_mgr._current_json("my-skill").write_text(pointer.to_json(), encoding="utf-8")

    context = MagicMock()
    context.llm = MagicMock()
    context.mailbox = AsyncMock()
    context.mailbox.deposit = AsyncMock(return_value=True)

    healthy_report = _make_healthy_report("my-skill", "1.0-evolve-202604")

    with patch("src.everbot.core.jobs.skill_evaluate.evaluate_skill", new=AsyncMock(return_value=healthy_report)):
        result = await _evaluate_one(context, seg_logger, ver_mgr, "my-skill", tmp_path / "sessions")

    # Should have activated
    meta = ver_mgr.get_metadata("my-skill", "1.0-evolve-202604")
    assert meta.status == VersionStatus.ACTIVE
    pointer = ver_mgr.get_pointer("my-skill")
    assert pointer.consecutive_evolve_count == 0
    context.mailbox.deposit.assert_awaited_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_skill_evaluate_job.py::test_testing_healthy_activates -v`
Expected: FAIL — no activation logic exists yet

- [ ] **Step 3: Write test for unhealthy→rollback+evolve path**

```python
# In tests/unit/test_skill_evaluate_job.py, add:

@pytest.mark.asyncio
async def test_unhealthy_triggers_rollback_and_evolve(tmp_path: Path):
    """Unhealthy report → rollback to stable + LLM evolve + publish testing."""
    skills_dir = tmp_path / "skills"
    logs_dir = tmp_path / "skill_logs"
    eval_dir = tmp_path / "skill_eval"
    skills_dir.mkdir()

    ver_mgr, seg_logger = _setup_skill_with_version(
        skills_dir, logs_dir, eval_dir, "bad-skill", "1.0", VersionStatus.ACTIVE,
    )

    context = MagicMock()
    context.llm = AsyncMock()
    # LLM returns valid SKILL.md for evolve
    context.llm.complete = AsyncMock(return_value=(
        "---\nname: bad-skill\nversion: \"1.0-evolve-fix\"\n---\nImproved content"
    ))
    context.mailbox = AsyncMock()
    context.mailbox.deposit = AsyncMock(return_value=True)

    unhealthy_report = _make_unhealthy_report("bad-skill", "1.0")

    with patch("src.everbot.core.jobs.skill_evaluate.evaluate_skill", new=AsyncMock(return_value=unhealthy_report)):
        result = await _evaluate_one(context, seg_logger, ver_mgr, "bad-skill", tmp_path / "sessions")

    # New version should be published as testing
    pointer = ver_mgr.get_pointer("bad-skill")
    assert "evolve" in pointer.current_version
    meta = ver_mgr.get_metadata("bad-skill", pointer.current_version)
    assert meta.status == VersionStatus.TESTING
    assert pointer.consecutive_evolve_count == 1
    # Mailbox should have been notified
    context.mailbox.deposit.assert_awaited()
```

- [ ] **Step 4: Write test for suspend path**

```python
# In tests/unit/test_skill_evaluate_job.py, add:

@pytest.mark.asyncio
async def test_evolve_count_exceeded_suspends(tmp_path: Path):
    """Consecutive evolve > MAX → suspend skill."""
    skills_dir = tmp_path / "skills"
    logs_dir = tmp_path / "skill_logs"
    eval_dir = tmp_path / "skill_eval"
    skills_dir.mkdir()

    ver_mgr, seg_logger = _setup_skill_with_version(
        skills_dir, logs_dir, eval_dir, "stuck-skill", "1.0", VersionStatus.ACTIVE,
    )
    # Set evolve count past limit
    pointer = ver_mgr.get_pointer("stuck-skill")
    pointer.consecutive_evolve_count = 3
    ver_mgr._current_json("stuck-skill").write_text(pointer.to_json(), encoding="utf-8")

    context = MagicMock()
    context.llm = MagicMock()
    context.mailbox = AsyncMock()
    context.mailbox.deposit = AsyncMock(return_value=True)

    unhealthy_report = _make_unhealthy_report("stuck-skill", "1.0")

    with patch("src.everbot.core.jobs.skill_evaluate.evaluate_skill", new=AsyncMock(return_value=unhealthy_report)):
        result = await _evaluate_one(context, seg_logger, ver_mgr, "stuck-skill", tmp_path / "sessions")

    # Should be suspended, not evolved
    meta = ver_mgr.get_metadata("stuck-skill", "1.0")
    assert meta.status == VersionStatus.SUSPENDED
    # Mailbox notified about suspension
    deposit_call = context.mailbox.deposit.call_args
    assert "暂停" in deposit_call.kwargs.get("summary", "") or "暂停" in deposit_call.args[0] if deposit_call.args else True
```

- [ ] **Step 5: Write test for evolve LLM failure (graceful degradation)**

```python
# In tests/unit/test_skill_evaluate_job.py, add:

@pytest.mark.asyncio
async def test_evolve_llm_failure_still_rolls_back(tmp_path: Path):
    """If LLM evolve fails, rollback still happens but no new version published."""
    skills_dir = tmp_path / "skills"
    logs_dir = tmp_path / "skill_logs"
    eval_dir = tmp_path / "skill_eval"
    skills_dir.mkdir()

    ver_mgr, seg_logger = _setup_skill_with_version(
        skills_dir, logs_dir, eval_dir, "fail-skill", "2.0", VersionStatus.ACTIVE,
    )

    context = MagicMock()
    context.llm = AsyncMock()
    context.llm.complete = AsyncMock(return_value="invalid garbage no frontmatter")
    context.mailbox = AsyncMock()
    context.mailbox.deposit = AsyncMock(return_value=True)

    unhealthy_report = _make_unhealthy_report("fail-skill", "2.0")

    with patch("src.everbot.core.jobs.skill_evaluate.evaluate_skill", new=AsyncMock(return_value=unhealthy_report)):
        result = await _evaluate_one(context, seg_logger, ver_mgr, "fail-skill", tmp_path / "sessions")

    # Rollback should have happened (current != 2.0)
    pointer = ver_mgr.get_pointer("fail-skill")
    assert pointer.current_version != "2.0"
    # No new testing version published (evolve failed)
    assert "evolve" not in pointer.current_version
```

- [ ] **Step 6: Run all new tests to verify they fail**

Run: `pytest tests/unit/test_skill_evaluate_job.py -k "testing_healthy or unhealthy_triggers or count_exceeded or llm_failure" -v`
Expected: All 4 FAIL

- [ ] **Step 7: Implement the evolve logic in skill_evaluate.py**

Replace the full content of `src/everbot/core/jobs/skill_evaluate.py`:

```python
"""Skill evaluation job — score skill invocations using LLM Judge.

Runs as a per-agent task, reads inline evaluation segments from the
agent's skill_logs/ directory, and produces eval_report.json in the
agent's skill_eval/ directory.

Post-evaluation: unhealthy skills are rolled back and improved via LLM.
Testing versions that pass evaluation are activated.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import List

from ..runtime.skill_context import SkillContext
from ..slm.judge import evaluate_skill
from ..slm.models import EvaluationSegment, EvalReport, VersionStatus
from ..slm.segment_logger import SegmentLogger
from ..slm.version_manager import VersionManager

logger = logging.getLogger(__name__)
_SKILL_EVALUATION_TIMEOUT_SECONDS = 120
MAX_CONSECUTIVE_EVOLVE = 2

_EVOLVE_SYSTEM = (
    "You are a skill improvement assistant. "
    "Given a skill definition and examples of failed invocations, "
    "output an improved version of the full skill file. "
    "Output ONLY the complete improved skill file content, nothing else."
)

_EVOLVE_PROMPT = """\
The following skill has been performing poorly. Analyze the failure cases and \
produce an improved version.

## Current Skill Definition

```
{skill_content}
```

## Failure Cases

{failure_block}

## Instructions

1. Analyze why the skill produced bad outputs in these cases.
2. Modify the skill definition to fix the identified issues.
3. Only change parts that caused the failures. Keep everything else intact.
4. The output must be a complete, valid skill file starting with `---` frontmatter.
5. Update the `version` field in the frontmatter to: "{new_version}"
"""


async def run(context: SkillContext) -> str:
    """Evaluate all skills that have accumulated new entries since last report."""
    from ...infra.user_data import get_user_data_manager

    udm = get_user_data_manager()

    # Use agent-scoped dirs from context, fall back to global for backward compat
    skill_logs_dir = context.skill_logs_dir or udm.skill_logs_dir
    skill_eval_dir = context.skill_eval_dir  # None → legacy .eval/ under skills_dir

    seg_logger = SegmentLogger(skill_logs_dir)
    ver_mgr = VersionManager(udm.skills_dir, eval_base_dir=skill_eval_dir)

    skill_ids = seg_logger.list_skills()
    if not skill_ids:
        return "HEARTBEAT_OK No skill logs found"

    from .llm_errors import LLMTransientError, LLMConfigError

    evaluated = 0
    unavailable = 0
    for skill_id in skill_ids:
        try:
            result = await _evaluate_one(
                context, seg_logger, ver_mgr, skill_id, udm.sessions_dir,
            )
            if result:
                evaluated += 1
                logger.info("Evaluated %s: %s", skill_id, result)
        except (LLMTransientError, LLMConfigError):
            unavailable += 1
            logger.warning("LLM unavailable during %s evaluation, skipping skill", skill_id)
        except Exception as e:
            logger.warning("Failed to evaluate %s: %s", skill_id, e)

    # Cleanup old entries
    for skill_id in skill_ids:
        try:
            seg_logger.cleanup(skill_id)
        except Exception as e:
            logger.warning("Cleanup failed for %s: %s", skill_id, e)

    summary = f"Evaluated {evaluated}/{len(skill_ids)} skills"
    if unavailable:
        summary += f", skipped {unavailable} due to LLM unavailability"
        return summary
    # Routine evaluation — suppress from user notification
    return f"HEARTBEAT_OK {summary}"


async def _evaluate_one(
    context: SkillContext,
    seg_logger: SegmentLogger,
    ver_mgr: VersionManager,
    skill_id: str,
    sessions_dir,
) -> str | None:
    """Evaluate a single skill. Returns summary string or None if skipped."""
    entries = seg_logger.load(skill_id)
    if not entries:
        return None

    # Find the current version to evaluate.
    pointer = ver_mgr.get_pointer(skill_id)
    if pointer:
        target_version = pointer.current_version
    else:
        from collections import Counter
        version_counts = Counter(e.skill_version for e in entries)
        target_version = version_counts.most_common(1)[0][0] if version_counts else "baseline"

    target_entries = [e for e in entries if e.skill_version == target_version]

    if not target_entries:
        return None

    # Check if we already have a report with same segment count
    existing = ver_mgr.get_eval_report(skill_id, target_version)
    if existing and existing.segment_count >= len(target_entries):
        return None  # already evaluated

    # Skip segments with no content
    segments = [e for e in target_entries if e.skill_output or e.context_before]
    if not segments:
        logger.info("No segments with content for %s v%s", skill_id, target_version)
        return None

    from .llm_errors import LLMTransientError

    try:
        report = await asyncio.wait_for(
            evaluate_skill(context.llm, skill_id, target_version, segments),
            timeout=_SKILL_EVALUATION_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise LLMTransientError(
            f"Request timed out during skill evaluation for {skill_id}"
        ) from exc
    ver_mgr.save_eval_report(skill_id, target_version, report)

    # --- Evolve loop ---
    await _post_evaluate(context, ver_mgr, seg_logger, skill_id, target_version, report)

    return (
        f"v{target_version}: {report.segment_count} segments, "
        f"critical={report.critical_issue_rate:.0%}, "
        f"satisfaction={report.mean_satisfaction:.2f}"
    )


async def _post_evaluate(
    context: SkillContext,
    ver_mgr: VersionManager,
    seg_logger: SegmentLogger,
    skill_id: str,
    target_version: str,
    report: EvalReport,
) -> None:
    """Post-evaluation actions: activate healthy testing, rollback+evolve unhealthy."""
    meta = ver_mgr.get_metadata(skill_id, target_version)
    if not meta:
        return

    # Testing version passed evaluation → activate
    if meta.status == VersionStatus.TESTING and report.is_healthy:
        ver_mgr.activate(skill_id, target_version)
        try:
            await context.mailbox.deposit(
                summary=f"技能 {skill_id} v{target_version} 验证通过，已生效",
                detail="",
            )
        except Exception:
            pass
        return

    # Healthy active version → nothing to do
    if report.is_healthy:
        return

    # --- Unhealthy: rollback + evolve ---
    pointer = ver_mgr.get_pointer(skill_id)

    # Check evolve count limit
    if pointer and pointer.consecutive_evolve_count > MAX_CONSECUTIVE_EVOLVE:
        meta.status = VersionStatus.SUSPENDED
        meta.suspended_reason = "consecutive evolve limit exceeded"
        ver_dir = ver_mgr._version_dir(skill_id, target_version)
        (ver_dir / "metadata.json").write_text(meta.to_json(), encoding="utf-8")
        try:
            await context.mailbox.deposit(
                summary=f"技能 {skill_id} 连续改进仍不达标，已暂停",
                detail=f"satisfaction={report.mean_satisfaction:.2f}, critical_rate={report.critical_issue_rate:.0%}",
            )
        except Exception:
            pass
        logger.warning("Suspended %s after %d consecutive evolve attempts", skill_id, pointer.consecutive_evolve_count)
        return

    # Rollback
    try:
        rolled_to = ver_mgr.rollback(skill_id, reason="auto-evolve: unhealthy evaluation")
    except ValueError as e:
        logger.warning("Cannot rollback %s: %s", skill_id, e)
        return

    # Evolve
    new_version = await _maybe_evolve(context, ver_mgr, seg_logger, skill_id, report)
    if new_version and pointer:
        pointer.consecutive_evolve_count += 1
        ver_mgr._current_json(skill_id).write_text(pointer.to_json(), encoding="utf-8")

    evolve_msg = f"改进为 v{new_version}，进入验证阶段" if new_version else "自动改进失败，已回退到稳定版本"
    try:
        await context.mailbox.deposit(
            summary=f"技能 {skill_id} 评估不达标，{evolve_msg}",
            detail=f"satisfaction={report.mean_satisfaction:.2f}, critical_rate={report.critical_issue_rate:.0%}",
        )
    except Exception:
        pass


async def _maybe_evolve(
    context: SkillContext,
    ver_mgr: VersionManager,
    seg_logger: SegmentLogger,
    skill_id: str,
    report: EvalReport,
) -> str | None:
    """Generate improved SKILL.md via LLM based on failure cases. Returns new version or None."""
    skill_md = ver_mgr._skill_md(skill_id)
    if not skill_md.exists():
        return None
    current_content = skill_md.read_text(encoding="utf-8")

    # Collect failed segments
    target_entries = seg_logger.load_by_version(skill_id, report.skill_version)
    failed: List[tuple[EvaluationSegment, str]] = []
    for entry, result in zip(target_entries, report.results):
        if result.has_critical_issue or result.satisfaction < 0.5:
            failed.append((entry, result.reason))
    if not failed:
        return None

    # Build version string
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    base = report.skill_version.split("-evolve-")[0]
    new_version = f"{base}-evolve-{ts}"

    # Build prompt
    failure_block = _build_failure_block(failed)
    prompt = _EVOLVE_PROMPT.format(
        skill_content=current_content,
        failure_block=failure_block,
        new_version=new_version,
    )

    try:
        new_content = await context.llm.complete(prompt, system=_EVOLVE_SYSTEM)
    except Exception as e:
        logger.warning("Evolve LLM call failed for %s: %s", skill_id, e)
        return None

    if not _validate_skill_md(new_content):
        logger.warning("Evolve output for %s failed validation", skill_id)
        return None

    ver_mgr.publish(skill_id, new_version, new_content)
    logger.info("Evolved %s to v%s", skill_id, new_version)
    return new_version


def _build_failure_block(failed: List[tuple[EvaluationSegment, str]]) -> str:
    """Format failed segments for the evolve prompt."""
    parts = []
    for i, (seg, reason) in enumerate(failed):
        parts.append(
            f"### Case {i + 1}\n"
            f"**User Input:** {seg.context_before or '(empty)'}\n"
            f"**Skill Output:** {seg.skill_output or '(empty)'}\n"
            f"**User Reaction:** {seg.context_after or '(no reaction recorded)'}\n"
            f"**Judge Assessment:** {reason}"
        )
    return "\n\n".join(parts)


def _validate_skill_md(content: str) -> bool:
    """Basic validation: non-empty, has frontmatter with version."""
    if not content or not content.strip():
        return False
    if not re.search(r"^---\s*\n", content):
        return False
    match = re.search(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return False
    frontmatter = match.group(1)
    if "version:" not in frontmatter:
        return False
    return True
```

- [ ] **Step 8: Run all tests**

Run: `pytest tests/unit/test_skill_evaluate_job.py -v`
Expected: All PASS (existing + 4 new tests)

- [ ] **Step 9: Run full SLM test suite to check for regressions**

Run: `pytest tests/unit/test_slm_models.py tests/unit/test_slm_version_manager.py tests/unit/test_slm_judge.py tests/unit/test_slm_segment_logger.py tests/unit/test_skill_evaluate_job.py -v`
Expected: All PASS

- [ ] **Step 10: Commit**

```bash
git add src/everbot/core/jobs/skill_evaluate.py tests/unit/test_skill_evaluate_job.py
git commit -m "feat(slm): add evolve loop — rollback, LLM improve, activate"
```

---

### Task 4: Integration smoke test

**Files:**
- No new files — manual verification

- [ ] **Step 1: Run the full unit test suite**

Run: `pytest tests/unit/ -x -q`
Expected: All PASS, no regressions

- [ ] **Step 2: Verify existing eval data is compatible**

```bash
python -c "
from src.everbot.core.slm.models import CurrentPointer
import json
from pathlib import Path

# Load an existing current.json (if any)
for p in Path.home().glob('.alfred/agents/*/skill_eval/*/current.json'):
    data = json.loads(p.read_text())
    pointer = CurrentPointer.from_dict(data)
    print(f'{p}: v={pointer.current_version} evolve_count={pointer.consecutive_evolve_count}')
    assert pointer.consecutive_evolve_count == 0, 'backward compat broken'

print('All existing pointers loaded successfully')
"
```
Expected: All pointers load with `evolve_count=0`

- [ ] **Step 3: Commit (if any fixups needed)**

Only if issues were found and fixed in previous steps.
