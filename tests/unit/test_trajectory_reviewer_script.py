from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path


def _load_review_recent_module():
    script_path = (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "trajectory-reviewer"
        / "scripts"
        / "review_recent.py"
    )
    spec = importlib.util.spec_from_file_location("trajectory_reviewer_review_recent", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_parse_args_defaults_to_two_files(monkeypatch):
    module = _load_review_recent_module()
    monkeypatch.setattr("sys.argv", ["review_recent.py"])
    args = module.parse_args()
    assert args.limit_files == 2


def test_discover_trajectory_files_prioritizes_primary_and_heartbeat(tmp_path):
    module = _load_review_recent_module()
    base = tmp_path / "agents" / "demo_agent" / "tmp"
    base.mkdir(parents=True)

    primary = base / "trajectory_tg_session_demo_agent__123.json"
    heartbeat = base / "trajectory_heartbeat_session_demo_agent.json"
    unrelated = base / "trajectory_job_routine_x.json"

    for path in (primary, heartbeat, unrelated):
        path.write_text("{}", encoding="utf-8")

    now = datetime.now().timestamp()
    primary.touch()
    heartbeat.touch()
    unrelated.touch()
    # Make unrelated newest, heartbeat oldest, primary in the middle.
    import os
    os.utime(unrelated, (now + 20, now + 20))
    os.utime(primary, (now + 10, now + 10))
    os.utime(heartbeat, (now, now))

    paths = module.discover_trajectory_files(str(base / "trajectory_*.json"), agent=None, session=None, limit=2)
    assert paths == [primary, heartbeat]


def test_discover_trajectory_files_agent_filter_tolerates_custom_glob(tmp_path):
    module = _load_review_recent_module()
    path = tmp_path / "custom" / "trajectory_demo.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")

    paths = module.discover_trajectory_files(str(path), agent="demo_agent", session=None, limit=2)
    assert paths == []


def test_analyze_trajectories_does_not_count_report_text_as_tool_error(tmp_path):
    module = _load_review_recent_module()
    path = tmp_path / "trajectory_tg_session_demo_agent__123.json"
    payload = {
        "trajectory": [
            {
                "role": "tool",
                "content": (
                    "# Trajectory Self-Review Report\n\n"
                    "## Findings\n"
                    "1. Repeated runtime error signature in log\n"
                    "   - Evidence: `message=Traceback (most recent call last):`\n"
                    "   - Evidence: `snippet=Command exited with code 2`\n"
                ),
            }
        ]
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    metrics, findings, _sessions = module.analyze_trajectories([path])
    assert metrics["tool_error_messages"] == 0
    assert all(f.title != "Frequent tool-level failures in trajectories" for f in findings)


def test_analyze_trajectories_reports_unreadable_json(tmp_path):
    module = _load_review_recent_module()
    path = tmp_path / "trajectory_bad.json"
    path.write_text("{not valid json", encoding="utf-8")

    metrics, findings, _sessions = module.analyze_trajectories([path])
    assert metrics["files"] == 1
    assert any(f.title == "Unreadable trajectory file" for f in findings)


def test_tail_lines_reads_only_requested_tail(tmp_path):
    module = _load_review_recent_module()
    path = tmp_path / "big.log"
    path.write_text("\n".join(f"line-{i}" for i in range(10)), encoding="utf-8")
    assert module.tail_lines(path, 3) == ["line-7", "line-8", "line-9"]
