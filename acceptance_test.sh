#!/bin/bash
set -euo pipefail

# Acceptance test script for: SLM Skill Log 写入适配层 (#2)
# FAILS before implementation (skill_log_recorder.py missing).
# PASSES after implementation.
#
# Run from project root: bash acceptance_test.sh

export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}."

# ---------------------------------------------------------------------------
# Bootstrap helper: pre-stub noisy __init__.py packages so that SLM modules
# (which are pure-stdlib) can be imported without triggering dolphin deps.
# ---------------------------------------------------------------------------
BOOT='
import sys, types
from pathlib import Path

def _stub_pkg(dotted, rel):
    m = types.ModuleType(dotted)
    m.__path__ = [str(Path(rel))]
    m.__package__ = dotted
    sys.modules[dotted] = m

_stub_pkg("src",                      "src")
_stub_pkg("src.everbot",              "src/everbot")
_stub_pkg("src.everbot.core",         "src/everbot/core")
_stub_pkg("src.everbot.core.runtime", "src/everbot/core/runtime")
'

# ---------------------------------------------------------------------------
echo "=== AC-1: only user-level skills are logged; internal tools are skipped ==="
# ---------------------------------------------------------------------------
python3 - <<PYEOF
$BOOT
import tempfile
from pathlib import Path

tmpdir = Path(tempfile.mkdtemp())
skill_logs_dir = tmpdir / "skill_logs"
skills_dir     = tmpdir / "skills"

from src.everbot.core.slm.skill_log_recorder import SkillLogRecorder
from src.everbot.core.slm.segment_logger import SegmentLogger

recorder = SkillLogRecorder(skill_logs_dir, skills_dir)

r_bash   = recorder.maybe_record("_bash",   session_id="sess-1")
r_python = recorder.maybe_record("_python", session_id="sess-1")
assert r_bash   is False, "_bash should return False, got %r" % r_bash
assert r_python is False, "_python should return False, got %r" % r_python

assert not (skill_logs_dir / "_bash.jsonl").exists(),   "_bash.jsonl must not be created"
assert not (skill_logs_dir / "_python.jsonl").exists(), "_python.jsonl must not be created"

r_ws = recorder.maybe_record("web-search", session_id="sess-1")
assert r_ws is True, "web-search should return True, got %r" % r_ws

skills = SegmentLogger(skill_logs_dir).list_skills()
assert skills == ["web-search"], "Expected ['web-search'], got %r" % skills

print("AC-1 PASSED")
PYEOF

# ---------------------------------------------------------------------------
echo "=== AC-2: handle_skill_event() with a completed TurnEvent triggers a log write ==="
# ---------------------------------------------------------------------------
python3 - <<PYEOF
$BOOT
import tempfile
from pathlib import Path

tmpdir = Path(tempfile.mkdtemp())
skill_logs_dir = tmpdir / "skill_logs"
skills_dir     = tmpdir / "skills"

from src.everbot.core.slm.skill_log_recorder import SkillLogRecorder, handle_skill_event
from src.everbot.core.slm.segment_logger import SegmentLogger
from src.everbot.core.runtime.turn_policy import TurnEvent, TurnEventType

recorder = SkillLogRecorder(skill_logs_dir, skills_dir)

event = TurnEvent(
    type=TurnEventType.SKILL,
    skill_name="paper-discovery",
    skill_output="Found 3 relevant papers.",
    status="completed",
)

result = handle_skill_event(event, recorder, session_id="sess-2", context_before="find papers")
assert result is True, "handle_skill_event should return True, got %r" % result

segments = SegmentLogger(skill_logs_dir).load("paper-discovery")
assert len(segments) == 1, "Expected 1 log entry, got %d" % len(segments)
assert segments[0].skill_id == "paper-discovery", \
    "skill_id mismatch: %r" % segments[0].skill_id

event_running = TurnEvent(
    type=TurnEventType.SKILL,
    skill_name="paper-discovery",
    status="running",
)
result_running = handle_skill_event(event_running, recorder, session_id="sess-2")
assert result_running is False, "running status should return False, got %r" % result_running
assert len(SegmentLogger(skill_logs_dir).load("paper-discovery")) == 1, \
    "running event must not add a second entry"

print("AC-2 PASSED")
PYEOF

# ---------------------------------------------------------------------------
echo "=== AC-3: skill_version from SKILL.md frontmatter; fallback to baseline ==="
# ---------------------------------------------------------------------------
python3 - <<PYEOF
$BOOT
import tempfile
from pathlib import Path

tmpdir = Path(tempfile.mkdtemp())
skill_logs_dir = tmpdir / "skill_logs"
skills_dir     = tmpdir / "skills"

from src.everbot.core.slm.skill_log_recorder import SkillLogRecorder
from src.everbot.core.slm.segment_logger import SegmentLogger

skill_md = skills_dir / "web-search" / "SKILL.md"
skill_md.parent.mkdir(parents=True, exist_ok=True)
skill_md.write_text(
    "---\nname: web-search\nversion: 2.1.0\n---\nDoes web searches.\n",
    encoding="utf-8",
)

recorder = SkillLogRecorder(skill_logs_dir, skills_dir)
recorder.maybe_record("web-search", session_id="sess-3", context_before="query")

segs = SegmentLogger(skill_logs_dir).load("web-search")
assert len(segs) == 1, "Expected 1 segment, got %d" % len(segs)
assert segs[0].skill_version == "2.1.0", \
    "Expected skill_version='2.1.0', got %r" % segs[0].skill_version

skill_md.unlink()

recorder2 = SkillLogRecorder(skill_logs_dir, skills_dir)
recorder2.maybe_record("web-search", session_id="sess-3b", context_before="query2")

segs2 = SegmentLogger(skill_logs_dir).load("web-search")
baseline_entries = [s for s in segs2 if s.skill_version == "baseline"]
assert len(baseline_entries) == 1, \
    "Expected 1 baseline entry after removing SKILL.md, got %r" % baseline_entries

print("AC-3 PASSED")
PYEOF

# ---------------------------------------------------------------------------
echo "=== AC-4: SegmentLogger.load() can read back what SkillLogRecorder wrote ==="
# ---------------------------------------------------------------------------
python3 - <<PYEOF
$BOOT
import tempfile
from pathlib import Path
from datetime import datetime

tmpdir = Path(tempfile.mkdtemp())
skill_logs_dir = tmpdir / "skill_logs"
skills_dir     = tmpdir / "skills"

from src.everbot.core.slm.skill_log_recorder import SkillLogRecorder
from src.everbot.core.slm.segment_logger import SegmentLogger

recorder = SkillLogRecorder(skill_logs_dir, skills_dir)
recorder.maybe_record(
    "web-search",
    session_id="sess-closedloop",
    skill_output="10 results found",
    context_before="search for transformers",
)

segs = SegmentLogger(skill_logs_dir).load("web-search")
assert len(segs) == 1, "Expected 1 EvaluationSegment, got %d" % len(segs)

seg = segs[0]
assert seg.skill_id == "web-search",        "skill_id wrong: %r" % seg.skill_id
assert seg.session_id == "sess-closedloop", "session_id wrong: %r" % seg.session_id
assert seg.triggered_at,                    "triggered_at must not be empty"
assert seg.context_before == "search for transformers", \
    "context_before wrong: %r" % seg.context_before
assert seg.skill_output == "10 results found", \
    "skill_output wrong: %r" % seg.skill_output

try:
    datetime.fromisoformat(seg.triggered_at.replace("Z", "+00:00"))
except ValueError as e:
    raise AssertionError("triggered_at is not ISO-8601: %r" % seg.triggered_at) from e

print("AC-4 PASSED")
PYEOF

# ---------------------------------------------------------------------------
echo "=== AC-5: record_skills_from_raw_events() parses heartbeat raw-dict format ==="
# ---------------------------------------------------------------------------
python3 - <<PYEOF
$BOOT
import tempfile
from pathlib import Path

tmpdir = Path(tempfile.mkdtemp())
skill_logs_dir = tmpdir / "skill_logs"
skills_dir     = tmpdir / "skills"

from src.everbot.core.slm.skill_log_recorder import SkillLogRecorder, record_skills_from_raw_events
from src.everbot.core.slm.segment_logger import SegmentLogger

recorder = SkillLogRecorder(skill_logs_dir, skills_dir)

# Matches the dict format produced by TurnExecutor._turn_event_to_raw()
raw_events = [
    {"_progress": [{"stage": "skill",
                    "skill_info": {"name": "summarize", "args": "{}"},
                    "answer": "Summary text",
                    "id": "pid-1",
                    "status": "completed"}]},
    {"_progress": [{"stage": "skill",
                    "skill_info": {"name": "summarize", "args": "{}"},
                    "answer": "",
                    "id": "pid-2",
                    "status": "running"}]},
    {"_progress": [{"stage": "skill",
                    "skill_info": {"name": "_bash", "args": "{}"},
                    "answer": "ok",
                    "id": "pid-3",
                    "status": "completed"}]},
    {"_progress": [{"stage": "llm", "delta": "hello", "answer": ""}]},
]

count = record_skills_from_raw_events(
    raw_events, recorder, session_id="sess-hb", context_before="heartbeat trigger"
)
assert count == 1, "Expected 1 recorded skill, got %d" % count

segs = SegmentLogger(skill_logs_dir).load("summarize")
assert len(segs) == 1, "Expected 1 segment for summarize, got %d" % len(segs)
assert segs[0].skill_output == "Summary text", \
    "skill_output mismatch: %r" % segs[0].skill_output

assert not (skill_logs_dir / "_bash.jsonl").exists(), "_bash.jsonl must not exist"

print("AC-5 PASSED")
PYEOF

echo ""
echo "All acceptance criteria PASSED."
