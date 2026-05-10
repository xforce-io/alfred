# User-Directed Skill Evolve Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a built-in `skill-evolver` skill that lets the agent rewrite a target skill's SKILL.md on explicit user instruction, publishing the result as a new testing version via existing SLM primitives.

**Architecture:** Pure skill — `skills/skill-evolver/SKILL.md` + two CLI scripts (`prepare.py`, `commit.py`). Scripts import `VersionManager` / `read_frontmatter_version` / `skill_lock` directly. Agent itself rewrites SKILL.md content between the two script calls. Zero framework code changes. New versions land in testing with `<base>-userevolve-<ts>` tag; auto evolve continues as the safety net.

**Tech Stack:** Python 3.12, alfred SLM (`src/everbot/core/slm/`), pytest, conventions from `skills/routine-manager/`.

**Spec:** `docs/superpowers/specs/2026-05-10-user-directed-skill-evolve-design.md`

---

## File Structure

```
skills/skill-evolver/
  SKILL.md                          ← intent + 3-step workflow doc
  scripts/
    prepare.py                      ← read current SKILL.md, generate new version
    commit.py                       ← validate + VersionManager.publish

tests/unit/
  test_skill_evolver_prepare.py     ← prepare.py unit tests
  test_skill_evolver_commit.py      ← commit.py unit tests
```

**No framework changes.** All needed primitives already public:
- `src/everbot/core/slm/version_manager.py:VersionManager.publish`
- `src/everbot/core/slm/version_manager.py:read_frontmatter_version`
- `src/everbot/core/slm/_atomic_io.py:skill_lock`
- `src/everbot/infra/user_data.py:UserDataManager.get_agent_*`

---

## Conventions

**CLI invocation pattern** (mirrors `skills/routine-manager/scripts/routine_cli.py`):
- `--workspace <path>` is `~/.alfred/agents/<agent_name>/`. Agent passes `$WORKSPACE_ROOT` (context variable). Scripts derive `agent_name = Path(workspace).name`.
- `sys.path` bootstrap: scripts walk up to repo root and prepend, so `from src.everbot.core.slm...` resolves.
- Scripts emit JSON to stdout on success, JSON error + non-zero exit on failure.

**Test invocation:**
- All tests run via `/Users/xupeng/dev/github/alfred/.venv/bin/python -m pytest <file> -v`
- Use `tmp_path` pytest fixture for isolated workspaces.

---

### Task 1: Create skill-evolver SKILL.md

**Files:**
- Create: `skills/skill-evolver/SKILL.md`
- Create: `skills/skill-evolver/scripts/` (empty directory; scripts come in later tasks)

- [ ] **Step 1: Create the directory**

```bash
mkdir -p /Users/xupeng/dev/github/alfred/skills/skill-evolver/scripts
```

- [ ] **Step 2: Write SKILL.md**

Create `skills/skill-evolver/SKILL.md` with this exact content:

````markdown
---
name: skill-evolver
description: Adjust an installed skill's SKILL.md based on explicit user instruction. Use when the user wants to change how a skill behaves — phrases like "把 X 改成 Y", "调整 X", "X 报告太长", "优化 X 的 prompt", "make X output Y instead". One target skill per invocation.
version: "1.0.0"
tags: [meta, slm, skill-management]
---

# Skill Evolver

Rewrites a target skill's SKILL.md per explicit user instruction and publishes a new **testing** version. The auto SLM evaluation loop remains the safety net — bad rewrites get rolled back automatically on the next 2h Skill Evaluate cycle.

## When To Use

Trigger when the user expresses an explicit adjustment intent for a specific skill:
- "把 paper-discovery 改成只显示 5 条"
- "调整 gray-rhino 的 prompt"
- "X 这个报告太长了，改短点"
- "优化 X 输出格式"
- "make web skill use bing instead"

Do **not** trigger when:
- User is reporting a bug (use `fix` skill instead)
- User is asking what a skill does (just describe it)
- User intent is unclear (ask for confirmation first)

## Workflow

Three deterministic steps. Do them all in order — no shortcuts.

### Step 1 — prepare

```bash
python skills/skill-evolver/scripts/prepare.py \
  --workspace "$WORKSPACE_ROOT" \
  --skill <target-skill-id>
```

Returns JSON to stdout:
```json
{
  "current_skill_md": "<full current SKILL.md content>",
  "new_version": "<base>-userevolve-<YYYYMMDDHHMM>",
  "tmp_file": "/abs/path/to/tmp/skill-evolver-<skill>-<ts>.md"
}
```

Read all three values. The `tmp_file` is where you must write the new SKILL.md content in step 2.

### Step 2 — rewrite (you do this directly)

Take `current_skill_md` and modify it per the user's instruction:
- Apply the user's requested change to the relevant section
- **Update the `version:` field in the frontmatter to the `new_version` from step 1** (this is mandatory)
- Keep all unrelated parts intact

Save the rewritten content to `tmp_file` using `_bash` heredoc:
```bash
cat > <tmp_file> <<'SKILL_EOF'
---
name: <skill-id>
version: "<new_version>"
...rest of frontmatter and body...
SKILL_EOF
```

### Step 3 — commit

```bash
python skills/skill-evolver/scripts/commit.py \
  --workspace "$WORKSPACE_ROOT" \
  --skill <target-skill-id> \
  --version <new_version> \
  --content-file <tmp_file>
```

Returns:
```json
{"status": "ok", "skill": "<skill-id>", "version": "<new>", "current_pointer": "<new>"}
```

If commit fails (frontmatter mismatch, validation error), the script exits non-zero and emits an error JSON. **Do not retry without consulting the error message.**

## After Commit

Reply to the user with:
- The new version number
- A one-line summary of what you changed
- A note that this is in `testing` — automatic SLM eval will validate the change on the next cycle

Example:
> 已经把 paper-discovery 改成只显示前 5 条，新版本 `2.0.0-userevolve-202605101630`（testing）。下次跑就是新版；如果输出有问题，下一轮 Skill Evaluate 会自动回退到 stable。

## Notes

- One skill per invocation. If the user wants to change multiple skills, run this skill once per target.
- Rewriting goes through `VersionManager.publish` with `skill_lock` — concurrent auto evolves serialize cleanly.
- `consecutive_evolve_count` resets to 0 on publish (user-directed is explicit intent, shouldn't inherit auto-evolve failure history).
````

- [ ] **Step 3: Commit**

```bash
git add skills/skill-evolver/SKILL.md
git commit -m "feat(skill-evolver): add SKILL.md describing user-directed evolve workflow"
```

---

### Task 2: prepare.py — base version extraction

**Files:**
- Create: `skills/skill-evolver/scripts/prepare.py`
- Test: `tests/unit/test_skill_evolver_prepare.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_skill_evolver_prepare.py`:

```python
"""Unit tests for skill-evolver prepare.py."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "skills" / "skill-evolver" / "scripts" / "prepare.py"


def _load_module():
    """Import prepare.py as a module without running its CLI."""
    spec = importlib.util.spec_from_file_location("skill_evolver_prepare", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["skill_evolver_prepare"] = module
    spec.loader.exec_module(module)
    return module


class TestExtractBase:
    def test_plain_version(self):
        m = _load_module()
        assert m._extract_base("2.0.0") == "2.0.0"

    def test_strips_evolve_suffix(self):
        m = _load_module()
        assert m._extract_base("2.0.0-evolve-202604260331") == "2.0.0"

    def test_strips_userevolve_suffix(self):
        m = _load_module()
        assert m._extract_base("2.0.0-userevolve-202605101630") == "2.0.0"

    def test_baseline_passthrough(self):
        m = _load_module()
        assert m._extract_base("baseline") == "baseline"


class TestNewVersion:
    def test_format(self):
        m = _load_module()
        # Patch datetime by passing an explicit timestamp arg
        v = m._new_version("2.0.0", ts="202605101630")
        assert v == "2.0.0-userevolve-202605101630"

    def test_strips_existing_suffix_first(self):
        m = _load_module()
        v = m._new_version("2.0.0-evolve-202604260331", ts="202605101630")
        assert v == "2.0.0-userevolve-202605101630"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
/Users/xupeng/dev/github/alfred/.venv/bin/python -m pytest tests/unit/test_skill_evolver_prepare.py -v
```

Expected: FAIL — `prepare.py` doesn't exist yet, `_load_module()` raises `FileNotFoundError`.

- [ ] **Step 3: Write minimal prepare.py**

Create `skills/skill-evolver/scripts/prepare.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
/Users/xupeng/dev/github/alfred/.venv/bin/python -m pytest tests/unit/test_skill_evolver_prepare.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_skill_evolver_prepare.py skills/skill-evolver/scripts/prepare.py
git commit -m "feat(skill-evolver): prepare.py base version extraction and new-version generation"
```

---

### Task 3: prepare.py — main flow with SKILL.md lookup and JSON output

**Files:**
- Modify: `skills/skill-evolver/scripts/prepare.py`
- Test: `tests/unit/test_skill_evolver_prepare.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/unit/test_skill_evolver_prepare.py`:

```python
import json
import subprocess


PYTHON = "/Users/xupeng/dev/github/alfred/.venv/bin/python"


def _seed_skill(skills_dir: Path, skill_id: str, content: str) -> Path:
    skill_dir = skills_dir / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


class TestPrepareCli:
    def test_returns_json_with_required_keys(self, tmp_path: Path):
        # Workspace at tmp_path/agents/test_agent
        workspace = tmp_path / "agents" / "test_agent"
        writable_skills = workspace / "skills"
        skill_md_content = (
            "---\n"
            'name: target-skill\n'
            'version: "1.5.0"\n'
            "---\n\n"
            "# Target Skill\n\nBody text.\n"
        )
        _seed_skill(writable_skills, "target-skill", skill_md_content)

        result = subprocess.run(
            [PYTHON, str(SCRIPT_PATH),
             "--workspace", str(workspace),
             "--skill", "target-skill"],
            capture_output=True, text=True, env={"ALFRED_HOME": str(tmp_path)},
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        assert payload["current_skill_md"] == skill_md_content
        assert payload["new_version"].startswith("1.5.0-userevolve-")
        assert payload["tmp_file"].endswith(".md")
        assert "skill-evolver-target-skill-" in payload["tmp_file"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
/Users/xupeng/dev/github/alfred/.venv/bin/python -m pytest tests/unit/test_skill_evolver_prepare.py::TestPrepareCli -v
```

Expected: FAIL — `prepare.py` has no CLI entrypoint yet.

- [ ] **Step 3: Add CLI main flow**

Append to `skills/skill-evolver/scripts/prepare.py`:

```python
import argparse
import json
import sys
from pathlib import Path


def _setup_import_path() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


def _resolve_skill_md(read_dirs: list[Path], skill_id: str) -> Path | None:
    """Walk the read priority chain; return first existing SKILL.md."""
    for d in read_dirs:
        p = d / skill_id / "SKILL.md"
        if p.exists():
            return p
    return None


def _err(msg: str, code: int = 1) -> None:
    """Emit an error JSON to stdout and exit non-zero."""
    print(json.dumps({"status": "error", "error": msg}))
    sys.exit(code)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="skill-evolver prepare step")
    parser.add_argument("--workspace", required=True, help="Agent workspace root (~/.alfred/agents/<agent>/)")
    parser.add_argument("--skill", required=True, help="Target skill id")
    args = parser.parse_args(argv)

    _setup_import_path()
    from src.everbot.infra.user_data import get_user_data_manager
    from src.everbot.core.slm.version_manager import read_frontmatter_version

    workspace = Path(args.workspace).resolve()
    agent_name = workspace.name

    udm = get_user_data_manager()
    read_dirs = udm.get_agent_read_skill_dirs(agent_name)

    skill_md = _resolve_skill_md(read_dirs, args.skill)
    if skill_md is None:
        _err(f"skill '{args.skill}' not found in any read layer for agent '{agent_name}'")

    current_content = skill_md.read_text(encoding="utf-8")
    current_version = read_frontmatter_version(skill_md)
    new_ver = _new_version(current_version)

    tmp_dir = workspace / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    ts = new_ver.rsplit("-", 1)[-1]
    tmp_file = tmp_dir / f"skill-evolver-{args.skill}-{ts}.md"

    print(json.dumps({
        "current_skill_md": current_content,
        "new_version": new_ver,
        "tmp_file": str(tmp_file),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

```bash
/Users/xupeng/dev/github/alfred/.venv/bin/python -m pytest tests/unit/test_skill_evolver_prepare.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_skill_evolver_prepare.py skills/skill-evolver/scripts/prepare.py
git commit -m "feat(skill-evolver): prepare.py CLI emits JSON with current SKILL.md and new version"
```

---

### Task 4: prepare.py — error path for missing skill

**Files:**
- Modify: `tests/unit/test_skill_evolver_prepare.py`

- [ ] **Step 1: Add the failing test**

Append to `TestPrepareCli` class in `tests/unit/test_skill_evolver_prepare.py`:

```python
    def test_missing_skill_returns_error_json(self, tmp_path: Path):
        workspace = tmp_path / "agents" / "test_agent"
        workspace.mkdir(parents=True)
        result = subprocess.run(
            [PYTHON, str(SCRIPT_PATH),
             "--workspace", str(workspace),
             "--skill", "nonexistent-skill"],
            capture_output=True, text=True, env={"ALFRED_HOME": str(tmp_path)},
        )
        assert result.returncode != 0
        payload = json.loads(result.stdout)
        assert payload["status"] == "error"
        assert "nonexistent-skill" in payload["error"]
```

- [ ] **Step 2: Run test to verify it passes**

```bash
/Users/xupeng/dev/github/alfred/.venv/bin/python -m pytest tests/unit/test_skill_evolver_prepare.py::TestPrepareCli::test_missing_skill_returns_error_json -v
```

Expected: PASS — `_err()` already handles this. (TDD discipline note: the test is already covered by the existing implementation. We verify it works rather than building, which is acceptable when the prior step's design naturally subsumes the case.)

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_skill_evolver_prepare.py
git commit -m "test(skill-evolver): lock in prepare.py error path for missing skill"
```

---

### Task 5: commit.py — frontmatter validation

**Files:**
- Create: `skills/skill-evolver/scripts/commit.py`
- Test: `tests/unit/test_skill_evolver_commit.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_skill_evolver_commit.py`:

```python
"""Unit tests for skill-evolver commit.py."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "skills" / "skill-evolver" / "scripts" / "commit.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("skill_evolver_commit", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["skill_evolver_commit"] = module
    spec.loader.exec_module(module)
    return module


class TestValidateContent:
    def test_valid_frontmatter_matches_expected_version(self):
        m = _load_module()
        content = (
            '---\n'
            'name: foo\n'
            'version: "2.0.0-userevolve-202605101630"\n'
            '---\n\n# Foo\n'
        )
        # No exception raised
        m._validate_content(content, expected_version="2.0.0-userevolve-202605101630")

    def test_missing_frontmatter_raises(self):
        m = _load_module()
        with pytest.raises(ValueError, match="frontmatter"):
            m._validate_content("# Just a body\n", expected_version="x")

    def test_missing_version_field_raises(self):
        m = _load_module()
        content = '---\nname: foo\n---\n\n# Foo\n'
        with pytest.raises(ValueError, match="version"):
            m._validate_content(content, expected_version="x")

    def test_version_mismatch_raises(self):
        m = _load_module()
        content = (
            '---\n'
            'name: foo\n'
            'version: "1.0.0"\n'
            '---\n'
        )
        with pytest.raises(ValueError, match="version mismatch"):
            m._validate_content(content, expected_version="2.0.0-userevolve-202605101630")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
/Users/xupeng/dev/github/alfred/.venv/bin/python -m pytest tests/unit/test_skill_evolver_commit.py -v
```

Expected: FAIL — `commit.py` doesn't exist.

- [ ] **Step 3: Write minimal commit.py**

Create `skills/skill-evolver/scripts/commit.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
/Users/xupeng/dev/github/alfred/.venv/bin/python -m pytest tests/unit/test_skill_evolver_commit.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_skill_evolver_commit.py skills/skill-evolver/scripts/commit.py
git commit -m "feat(skill-evolver): commit.py frontmatter validation"
```

---

### Task 6: commit.py — VersionManager.publish integration

**Files:**
- Modify: `skills/skill-evolver/scripts/commit.py`
- Test: `tests/unit/test_skill_evolver_commit.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/unit/test_skill_evolver_commit.py`:

```python
import json
import subprocess


PYTHON = "/Users/xupeng/dev/github/alfred/.venv/bin/python"


def _seed_workspace(tmp_path: Path, agent_name: str, skill_id: str, baseline_content: str) -> Path:
    """Create a minimal alfred home + agent workspace + writable skill dir."""
    workspace = tmp_path / "agents" / agent_name
    writable_skills = workspace / "skills" / skill_id
    writable_skills.mkdir(parents=True)
    (writable_skills / "SKILL.md").write_text(baseline_content, encoding="utf-8")
    # Eval dir gets created lazily by VersionManager.publish.
    return workspace


class TestCommitCli:
    def test_publish_writes_new_version_and_updates_pointer(self, tmp_path: Path):
        baseline = (
            '---\n'
            'name: target-skill\n'
            'version: "1.0.0"\n'
            '---\n\n# Target\nbaseline body\n'
        )
        workspace = _seed_workspace(tmp_path, "test_agent", "target-skill", baseline)

        new_version = "1.0.0-userevolve-202605101630"
        new_content = (
            '---\n'
            'name: target-skill\n'
            f'version: "{new_version}"\n'
            '---\n\n# Target\nrewritten body\n'
        )
        content_file = tmp_path / "rewritten.md"
        content_file.write_text(new_content, encoding="utf-8")

        result = subprocess.run(
            [PYTHON, str(SCRIPT_PATH),
             "--workspace", str(workspace),
             "--skill", "target-skill",
             "--version", new_version,
             "--content-file", str(content_file)],
            capture_output=True, text=True, env={"ALFRED_HOME": str(tmp_path)},
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["status"] == "ok"
        assert payload["version"] == new_version
        assert payload["current_pointer"] == new_version

        # SKILL.md is overwritten in the writable layer
        live = workspace / "skills" / "target-skill" / "SKILL.md"
        assert "rewritten body" in live.read_text(encoding="utf-8")

        # Snapshot and pointer exist in skill_eval
        snapshot = workspace / "skill_eval" / "target-skill" / "versions" / f"v{new_version}" / "skill.md"
        assert snapshot.exists()
        pointer = json.loads(
            (workspace / "skill_eval" / "target-skill" / "current.json").read_text(encoding="utf-8")
        )
        assert pointer["current_version"] == new_version
        assert pointer["consecutive_evolve_count"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
/Users/xupeng/dev/github/alfred/.venv/bin/python -m pytest tests/unit/test_skill_evolver_commit.py::TestCommitCli -v
```

Expected: FAIL — no CLI entrypoint in commit.py yet.

- [ ] **Step 3: Add the CLI main flow**

Append to `skills/skill-evolver/scripts/commit.py`:

```python
import argparse
import json
import sys
from pathlib import Path


def _setup_import_path() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


def _err(msg: str, code: int = 1) -> None:
    print(json.dumps({"status": "error", "error": msg}))
    sys.exit(code)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="skill-evolver commit step")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--skill", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--content-file", required=True)
    args = parser.parse_args(argv)

    _setup_import_path()
    from src.everbot.infra.user_data import get_user_data_manager
    from src.everbot.core.slm.version_manager import VersionManager
    from src.everbot.core.slm._atomic_io import skill_lock

    content_path = Path(args.content_file)
    if not content_path.exists():
        _err(f"content file not found: {content_path}")
    content = content_path.read_text(encoding="utf-8")

    try:
        _validate_content(content, expected_version=args.version)
    except ValueError as e:
        _err(str(e))

    workspace = Path(args.workspace).resolve()
    agent_name = workspace.name
    udm = get_user_data_manager()
    writable = udm.get_agent_writable_skills_dir(agent_name)
    eval_base = udm.get_agent_skill_eval_dir(agent_name)
    read_dirs = udm.get_agent_read_skill_dirs(agent_name)

    vm = VersionManager(
        skills_dir=writable,
        eval_base_dir=eval_base,
        read_skill_dirs=read_dirs,
    )

    lock_path = eval_base / args.skill / ".lock"
    with skill_lock(lock_path):
        try:
            vm.publish(args.skill, args.version, content)
        except Exception as e:
            _err(f"publish failed: {e}")
        pointer = vm.get_pointer(args.skill)

    print(json.dumps({
        "status": "ok",
        "skill": args.skill,
        "version": args.version,
        "current_pointer": pointer.current_version if pointer else "",
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

```bash
/Users/xupeng/dev/github/alfred/.venv/bin/python -m pytest tests/unit/test_skill_evolver_commit.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_skill_evolver_commit.py skills/skill-evolver/scripts/commit.py
git commit -m "feat(skill-evolver): commit.py invokes VersionManager.publish under skill_lock"
```

---

### Task 7: commit.py — error paths (missing content file, version mismatch via CLI)

**Files:**
- Modify: `tests/unit/test_skill_evolver_commit.py`

- [ ] **Step 1: Add the failing tests**

Append to `TestCommitCli` class in `tests/unit/test_skill_evolver_commit.py`:

```python
    def test_missing_content_file_returns_error(self, tmp_path: Path):
        workspace = _seed_workspace(
            tmp_path, "test_agent", "target-skill",
            '---\nname: target-skill\nversion: "1.0.0"\n---\n',
        )
        result = subprocess.run(
            [PYTHON, str(SCRIPT_PATH),
             "--workspace", str(workspace),
             "--skill", "target-skill",
             "--version", "1.0.0-userevolve-202605101630",
             "--content-file", str(tmp_path / "does-not-exist.md")],
            capture_output=True, text=True, env={"ALFRED_HOME": str(tmp_path)},
        )
        assert result.returncode != 0
        payload = json.loads(result.stdout)
        assert payload["status"] == "error"
        assert "content file not found" in payload["error"]

    def test_version_mismatch_returns_error(self, tmp_path: Path):
        baseline = '---\nname: target-skill\nversion: "1.0.0"\n---\n'
        workspace = _seed_workspace(tmp_path, "test_agent", "target-skill", baseline)
        bad_content = (
            '---\nname: target-skill\nversion: "1.5.0"\n---\n\nbody\n'
        )
        content_file = tmp_path / "bad.md"
        content_file.write_text(bad_content, encoding="utf-8")

        result = subprocess.run(
            [PYTHON, str(SCRIPT_PATH),
             "--workspace", str(workspace),
             "--skill", "target-skill",
             "--version", "1.0.0-userevolve-202605101630",
             "--content-file", str(content_file)],
            capture_output=True, text=True, env={"ALFRED_HOME": str(tmp_path)},
        )
        assert result.returncode != 0
        payload = json.loads(result.stdout)
        assert payload["status"] == "error"
        assert "mismatch" in payload["error"]
```

- [ ] **Step 2: Run tests to verify they pass**

```bash
/Users/xupeng/dev/github/alfred/.venv/bin/python -m pytest tests/unit/test_skill_evolver_commit.py::TestCommitCli -v
```

Expected: 3 passed (existing 1 + new 2). The error paths are covered by `_err()` and `_validate_content()` already; this task locks them in.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_skill_evolver_commit.py
git commit -m "test(skill-evolver): lock in commit.py error paths for missing file and version mismatch"
```

---

### Task 8: End-to-end smoke test

**Files:**
- Create: `tests/unit/test_skill_evolver_e2e.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_skill_evolver_e2e.py`:

```python
"""End-to-end smoke test for skill-evolver: prepare → write → commit."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PREPARE = REPO_ROOT / "skills" / "skill-evolver" / "scripts" / "prepare.py"
COMMIT = REPO_ROOT / "skills" / "skill-evolver" / "scripts" / "commit.py"
PYTHON = "/Users/xupeng/dev/github/alfred/.venv/bin/python"


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
        [PYTHON, str(PREPARE),
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
        [PYTHON, str(COMMIT),
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
```

- [ ] **Step 2: Run test to verify it passes**

```bash
/Users/xupeng/dev/github/alfred/.venv/bin/python -m pytest tests/unit/test_skill_evolver_e2e.py -v
```

Expected: 1 passed.

- [ ] **Step 3: Run full skill-evolver suite**

```bash
/Users/xupeng/dev/github/alfred/.venv/bin/python -m pytest tests/unit/test_skill_evolver_prepare.py tests/unit/test_skill_evolver_commit.py tests/unit/test_skill_evolver_e2e.py -v
```

Expected: all passed (target ~13 total).

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_skill_evolver_e2e.py
git commit -m "test(skill-evolver): end-to-end prepare → write → commit smoke test"
```

---

### Task 9: Manual integration check on demo_agent

**Files:** none (manual verification step)

- [ ] **Step 1: Inspect target skill state**

```bash
ls -lat ~/.alfred/agents/demo_agent/skill_eval/paper-discovery/versions/
cat ~/.alfred/agents/demo_agent/skill_eval/paper-discovery/current.json
```

Note the current_version and version dir mtimes for comparison.

- [ ] **Step 2: Run prepare.py against demo_agent's paper-discovery**

```bash
/Users/xupeng/dev/github/alfred/.venv/bin/python skills/skill-evolver/scripts/prepare.py \
  --workspace ~/.alfred/agents/demo_agent \
  --skill paper-discovery
```

Expected: JSON with `current_skill_md` (full text of paper-discovery's SKILL.md), `new_version` like `2.0.0-userevolve-...`, and `tmp_file` path under `~/.alfred/agents/demo_agent/tmp/`.

- [ ] **Step 3: Write a tiny rewrite to the suggested tmp file**

Take the `current_skill_md` from prepare's output. Edit only the frontmatter `version:` line to the new version, leave everything else identical. Save to the suggested `tmp_file`.

```bash
# Example — actual content depends on prepare.py output
cat > <tmp_file> <<'EOF'
<paste current_skill_md here, with version line replaced>
EOF
```

- [ ] **Step 4: Run commit.py**

```bash
/Users/xupeng/dev/github/alfred/.venv/bin/python skills/skill-evolver/scripts/commit.py \
  --workspace ~/.alfred/agents/demo_agent \
  --skill paper-discovery \
  --version <new_version> \
  --content-file <tmp_file>
```

Expected: `{"status": "ok", "skill": "paper-discovery", "version": "...", "current_pointer": "..."}`

- [ ] **Step 5: Verify state after commit**

```bash
cat ~/.alfred/agents/demo_agent/skill_eval/paper-discovery/current.json
ls ~/.alfred/agents/demo_agent/skill_eval/paper-discovery/versions/
head -5 ~/.alfred/agents/demo_agent/skills/paper-discovery/SKILL.md
```

Expected:
- `current.json` shows the new version as `current_version`, previous version as `stable_version`, `consecutive_evolve_count: 0`.
- New version dir present under `versions/`.
- Live SKILL.md frontmatter version matches the new version.

- [ ] **Step 6: (Optional rollback for clean state)**

If you want to revert the manual test:

```bash
/Users/xupeng/dev/github/alfred/.venv/bin/python -c "
from src.everbot.infra.user_data import get_user_data_manager
from src.everbot.core.slm.version_manager import VersionManager
udm = get_user_data_manager()
agent = 'demo_agent'
vm = VersionManager(
    skills_dir=udm.get_agent_writable_skills_dir(agent),
    eval_base_dir=udm.get_agent_skill_eval_dir(agent),
    read_skill_dirs=udm.get_agent_read_skill_dirs(agent),
)
print(vm.rollback('paper-discovery', reason='manual test cleanup'))
"
```

- [ ] **Step 7: Tag the verification in commit log**

```bash
git commit --allow-empty -m "verify(skill-evolver): manual end-to-end run on demo_agent paper-discovery"
```

---

## Self-Review Notes

**Spec coverage:**
- ✅ Section 3 (architecture, files, no framework changes) — Task 1 + structure header
- ✅ Section 4 (data flow) — Tasks 1 (SKILL.md) + 2-7 (scripts)
- ✅ Section 5.1 (SKILL.md content) — Task 1
- ✅ Section 5.2 (prepare.py) — Tasks 2-4
- ✅ Section 5.3 (commit.py) — Tasks 5-7. `consecutive_evolve_count` reset is asserted in Task 6 test.
- ✅ Section 6 (boundary with auto evolve) — version tag prefix `-userevolve-` baked into `_new_version` (Task 2); skill_lock used in commit.py (Task 6).
- ✅ Section 7 (watchpoint) — documented in spec, not implemented in v1 per YAGNI.
- ✅ Section 8 (YAGNI) — no slash command, no intent classifier, no diff confirm, no framework changes.
- ✅ Section 9 (testing) — Tasks 2-8 cover unit + e2e.
- ✅ Section 10 (files touched) — exactly matches plan structure.
- ✅ Section 11 (rollout) — Task 9 manual verification on demo_agent stands in for "first agent verification".

**Type consistency:** `_new_version`, `_extract_base`, `_validate_content` signatures consistent across tasks. JSON keys (`current_skill_md`, `new_version`, `tmp_file`, `status`, `skill`, `version`, `current_pointer`) consistent across SKILL.md doc and tests.

**No placeholders:** all code blocks complete. No "TBD"/"TODO"/"similar to". Manual verification task gives concrete commands.
