"""End-to-end smoke test for skill-evolver: prepare → write → commit."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PREPARE = REPO_ROOT / "skills" / "skill-evolver" / "scripts" / "prepare.py"
COMMIT = REPO_ROOT / "skills" / "skill-evolver" / "scripts" / "commit.py"


def test_prepare_then_commit_publishes_new_version(tmp_path: Path):
    # Arrange: workspace + writable skill
    workspace = tmp_path / "agents" / "smoke_agent"
    writable = workspace / "skills" / "demo-skill"
    writable.mkdir(parents=True)
    baseline = (
        '---\n'
        'name: demo-skill\n'
        'version: "0.5.0"\n'
        '---\n\n# Demo\nold body\n'
    )
    (writable / "SKILL.md").write_text(baseline, encoding="utf-8")

    env = {"ALFRED_HOME": str(tmp_path)}

    # Act 1: prepare
    prep = subprocess.run(
        [sys.executable, str(PREPARE),
         "--workspace", str(workspace),
         "--skill", "demo-skill"],
        capture_output=True, text=True, env=env,
    )
    assert prep.returncode == 0, prep.stderr
    payload = json.loads(prep.stdout)
    new_ver = payload["new_version"]
    tmp_file = Path(payload["tmp_file"])

    # Act 2: simulate the agent rewriting the file
    rewritten = (
        '---\n'
        'name: demo-skill\n'
        f'version: "{new_ver}"\n'
        '---\n\n# Demo\nNEW body — adjusted per user request\n'
    )
    tmp_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file.write_text(rewritten, encoding="utf-8")

    # Act 3: commit
    com = subprocess.run(
        [sys.executable, str(COMMIT),
         "--workspace", str(workspace),
         "--skill", "demo-skill",
         "--version", new_ver,
         "--content-file", str(tmp_file)],
        capture_output=True, text=True, env=env,
    )
    assert com.returncode == 0, com.stderr
    out = json.loads(com.stdout)
    assert out["status"] == "ok"
    assert out["version"] == new_ver

    # Assert: live SKILL.md, snapshot, pointer all reflect new version
    live = (writable / "SKILL.md").read_text(encoding="utf-8")
    assert "NEW body" in live
    assert new_ver in live

    snapshot = workspace / "skill_eval" / "demo-skill" / "versions" / f"v{new_ver}" / "skill.md"
    assert "NEW body" in snapshot.read_text(encoding="utf-8")

    pointer = json.loads(
        (workspace / "skill_eval" / "demo-skill" / "current.json").read_text(encoding="utf-8")
    )
    assert pointer["current_version"] == new_ver
    assert pointer["consecutive_evolve_count"] == 0
