"""Tests for scripts/slm_ensure_all.py CLI."""

import json
import subprocess
import sys
from pathlib import Path


def test_ensures_all_skills_in_dir(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    eval_dir = tmp_path / "eval"
    skills_dir.mkdir()
    eval_dir.mkdir()
    for name in ("alpha", "beta"):
        (skills_dir / name).mkdir()
        (skills_dir / name / "SKILL.md").write_text(
            f'---\nname: {name}\nversion: "1.0.0"\n---\nbody\n'
        )

    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "slm_ensure_all.py"),
         "--skills-dir", str(skills_dir),
         "--eval-dir", str(eval_dir),
         "--json"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["summary"]["bootstrapped"] == 2
    assert (eval_dir / "alpha" / "current.json").exists()
    assert (eval_dir / "beta" / "current.json").exists()


def test_idempotent_second_run_reports_noop(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    eval_dir = tmp_path / "eval"
    skills_dir.mkdir()
    eval_dir.mkdir()
    (skills_dir / "alpha").mkdir()
    (skills_dir / "alpha" / "SKILL.md").write_text(
        '---\nname: alpha\nversion: "1.0.0"\n---\nbody\n'
    )
    repo_root = Path(__file__).resolve().parents[2]
    cmd = [sys.executable, str(repo_root / "scripts" / "slm_ensure_all.py"),
           "--skills-dir", str(skills_dir),
           "--eval-dir", str(eval_dir),
           "--json"]
    # First run
    first = subprocess.run(cmd, capture_output=True, text=True)
    assert first.returncode == 0
    first_out = json.loads(first.stdout)
    assert first_out["summary"].get("bootstrapped") == 1
    # Second run — should be noop
    second = subprocess.run(cmd, capture_output=True, text=True)
    assert second.returncode == 0
    second_out = json.loads(second.stdout)
    assert second_out["summary"].get("noop") == 1
    assert "bootstrapped" not in second_out["summary"] or second_out["summary"]["bootstrapped"] == 0


def test_skips_non_skill_dirs(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    eval_dir = tmp_path / "eval"
    skills_dir.mkdir()
    eval_dir.mkdir()
    # Valid skill
    (skills_dir / "alpha").mkdir()
    (skills_dir / "alpha" / "SKILL.md").write_text(
        '---\nname: alpha\nversion: "1.0.0"\n---\nbody\n'
    )
    # Dir without SKILL.md (should be skipped)
    (skills_dir / "nope").mkdir()
    # File, not dir
    (skills_dir / "README.md").write_text("not a skill")

    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "slm_ensure_all.py"),
         "--skills-dir", str(skills_dir),
         "--eval-dir", str(eval_dir),
         "--json"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert len(out["skills"]) == 1
    assert out["skills"][0]["skill_id"] == "alpha"
