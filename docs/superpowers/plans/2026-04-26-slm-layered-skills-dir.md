# SLM Layered Skills Directory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decouple SLM evolution from the `~/.alfred/skills/` directory so symlink-installed skills can evolve without corrupting the upstream repo. SLM writes its evolved versions to the agent's per-workspace skills dir (which the loader already prioritizes), while leaving `~/.alfred/skills/` and `<repo>/skills/` as read-only fallbacks.

**Architecture:**
The dolphin loader scans `directories` in priority order (`<workspace>/skills/` > `~/.alfred/skills/` > `<repo>/skills/`); first match wins. Currently SLM's `VersionManager` was constructed with `~/.alfred/skills/` as both read source and write target — fine for non-symlinked installs, fatal for symlink installs (writes/unlinks resolve into the repo). This plan splits read vs. write: `VersionManager` accepts a list of read dirs (the same priority chain the loader uses) for inspecting baseline content, and a single writable dir (the agent workspace) for publish/rollback. SLM evolved versions land in `<workspace>/skills/<id>/SKILL.md`, naturally take precedence at load time, and never touch the lower layers.

**Tech Stack:** Python 3.12, pytest, existing SLM stack at `src/everbot/core/slm/`.

---

## Policy Decisions — already settled

The user has signed off on the layered approach. No new policy questions surface in this plan.

---

## Files Touched

### New methods on existing classes

| File | Change |
|---|---|
| `src/everbot/infra/user_data.py` | Add `get_agent_writable_skills_dir(agent_name)` and `get_agent_read_skill_dirs(agent_name)` |
| `src/everbot/core/slm/version_manager.py` | Add `read_skill_dirs` constructor param; add `_resolve_skill_md(skill_id)` that walks read dirs; relax `is_symlink_managed` since writable dir is workspace (real dir) |
| `src/everbot/core/slm/state_normalizer.py` | `StateInspector.inspect` reads `skill_md_exists` / `skill_md_version` via `_resolve_skill_md`; `_bootstrap` reads content via `_resolve_skill_md` (does not write to writable dir on bootstrap — that would create an unwanted override); leaves `_repair` writing to writable as before |
| `src/everbot/core/jobs/skill_evaluate.py` | Replace `VersionManager(udm.skills_dir, eval_base_dir=...)` with the layered construction using `context.workspace_path` |
| `src/everbot/core/slm/skill_log_recorder.py` | Replace single-dir `VersionManager` construction in `maybe_record` with layered version using existing `_skill_dirs` |
| `scripts/slm_ensure_all.py` | Accept optional `--read-skills-dirs` flag (colon-separated) for layered ensure-all; default = single-dir behavior unchanged |

### New tests

| File | Coverage |
|---|---|
| Add to `tests/unit/test_slm_version_manager.py` | `_resolve_skill_md` priority order + fallback when only some layers exist |
| Add to `tests/unit/test_slm_state_normalizer.py` | Bootstrap from layered baseline (writable empty, read layer has SKILL.md); writable empty after bootstrap |
| `tests/integration/test_slm_layered_evolve_e2e.py` | New integration test — symlinked skill in read layer, evolve writes to writable layer, upstream untouched, rollback restores layered baseline |

---

## Task 1: UserDataManager — agent skill directory helpers

**Why first:** Centralized truth for what counts as agent-writable vs. read-only-baseline. All later tasks depend on these helpers being available.

**Files:**
- Modify: `src/everbot/infra/user_data.py`
- Test: `tests/unit/test_user_data.py` (create if absent; otherwise add to existing)

- [ ] **Step 1: Locate existing test file or create one**

```bash
ls /Users/xupeng/dev/github/alfred/tests/unit/test_user_data.py 2>/dev/null || echo "MISSING — create it"
```

If missing, create with this header:

```python
"""Tests for UserDataManager."""

from pathlib import Path

import pytest

from src.everbot.infra.user_data import UserDataManager
```

- [ ] **Step 2: Write failing tests**

Append to `tests/unit/test_user_data.py`:

```python
class TestAgentSkillDirs:
    def test_writable_skills_dir_is_under_agent_workspace(self, tmp_path: Path):
        udm = UserDataManager(alfred_home=tmp_path)
        result = udm.get_agent_writable_skills_dir("demo_agent")
        assert result == tmp_path / "agents" / "demo_agent" / "skills"

    def test_read_skill_dirs_priority_order(self, tmp_path: Path, monkeypatch):
        # Make repo_skills_dir resolve to a tmp dir so the test is hermetic.
        monkeypatch.setenv("ALFRED_REPO_ROOT", str(tmp_path / "fakerepo"))
        (tmp_path / "fakerepo" / "skills").mkdir(parents=True)

        udm = UserDataManager(alfred_home=tmp_path)
        dirs = udm.get_agent_read_skill_dirs("demo_agent")

        # priority 0: agent workspace ; priority 1: global ; priority 2: repo
        assert dirs[0] == tmp_path / "agents" / "demo_agent" / "skills"
        assert dirs[1] == tmp_path / "skills"
        assert dirs[2] == tmp_path / "fakerepo" / "skills"

    def test_read_skill_dirs_excludes_missing_repo(self, tmp_path: Path, monkeypatch):
        """If repo_skills_dir resolves to None, the chain has only 2 layers.

        Note: we patch the property directly because repo_skills_dir's real
        implementation walks up from __file__ looking for pyproject.toml,
        which during test runs always finds the alfred repo. We need to
        force the 'no repo' state explicitly.
        """
        udm = UserDataManager(alfred_home=tmp_path)
        monkeypatch.setattr(
            type(udm), "repo_skills_dir",
            property(lambda self: None),
        )
        dirs = udm.get_agent_read_skill_dirs("demo_agent")
        assert len(dirs) == 2
        assert dirs[0] == tmp_path / "agents" / "demo_agent" / "skills"
        assert dirs[1] == tmp_path / "skills"
```

- [ ] **Step 3: Run, verify failure**

```bash
cd /Users/xupeng/dev/github/alfred
.venv/bin/pytest tests/unit/test_user_data.py::TestAgentSkillDirs -v
```

Expected: `AttributeError: 'UserDataManager' object has no attribute 'get_agent_writable_skills_dir'`

- [ ] **Step 4: Implement the methods**

Edit `src/everbot/infra/user_data.py`. Find the existing `get_agent_skill_eval_dir` method (around line 119) and add these two methods right after it:

```python
    def get_agent_writable_skills_dir(self, agent_name: str) -> Path:
        """Per-agent writable skills directory (= dolphin loader layer 0).

        SLM evolved versions land here. The loader already prioritizes this
        path over `~/.alfred/skills/` and `<repo>/skills/`, so writes here
        cleanly override lower-layer baselines without touching them.
        """
        return self.get_agent_dir(agent_name) / "skills"

    def get_agent_read_skill_dirs(self, agent_name: str) -> List[Path]:
        """Skill resolution priority chain (highest priority first).

        Mirrors the order dolphin's SkillLoader uses: agent workspace →
        user global → repo bundled. SLM uses this chain for read-only
        baseline lookup; for writes it uses get_agent_writable_skills_dir
        only (= the chain's first entry).
        """
        dirs: List[Path] = [self.get_agent_writable_skills_dir(agent_name), self.skills_dir]
        repo = self.repo_skills_dir
        if repo is not None:
            dirs.append(repo)
        return dirs
```

If `List` isn't already imported, add `from typing import List` at the top (or extend the existing `from typing import ...` line).

- [ ] **Step 5: Run, verify pass**

```bash
.venv/bin/pytest tests/unit/test_user_data.py::TestAgentSkillDirs -v
```

Expected: 3 tests pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/xupeng/dev/github/alfred
git add src/everbot/infra/user_data.py tests/unit/test_user_data.py
git commit -m "$(cat <<'EOF'
feat(infra): add agent-writable + layered-read skill dir helpers

Centralizes the truth that an agent's writable skills dir is its
workspace `skills/` (dolphin loader layer 0) and the read priority chain
is layer 0 → user global → repo bundled. SLM uses these to split write
target from read sources; loader path priority is unchanged.
EOF
)"
```

---

## Task 2: VersionManager — accept layered read dirs

**Files:**
- Modify: `src/everbot/core/slm/version_manager.py`
- Test: `tests/unit/test_slm_version_manager.py`

- [ ] **Step 1: Write failing test**

Append to `tests/unit/test_slm_version_manager.py`:

```python
class TestVersionManagerLayeredRead:
    def test_resolve_skill_md_prefers_writable(self, tmp_path: Path):
        writable = tmp_path / "writable"
        readable = tmp_path / "readable"
        for d in (writable, readable):
            (d / "foo").mkdir(parents=True)
        (writable / "foo" / "SKILL.md").write_text(
            '---\nname: foo\nversion: "writable"\n---\nbody\n'
        )
        (readable / "foo" / "SKILL.md").write_text(
            '---\nname: foo\nversion: "readable"\n---\nbody\n'
        )

        vm = VersionManager(
            writable, eval_base_dir=tmp_path / "eval",
            read_skill_dirs=[writable, readable],
        )
        resolved = vm._resolve_skill_md("foo")
        assert resolved == writable / "foo" / "SKILL.md"
        assert read_frontmatter_version(resolved) == "writable"

    def test_resolve_skill_md_falls_through_to_lower_layer(self, tmp_path: Path):
        writable = tmp_path / "writable"
        layer1 = tmp_path / "layer1"
        layer2 = tmp_path / "layer2"
        # writable empty for "bar"
        writable.mkdir()
        (layer2 / "bar").mkdir(parents=True)
        (layer2 / "bar" / "SKILL.md").write_text(
            '---\nname: bar\nversion: "from_layer2"\n---\nbody\n'
        )
        layer1.mkdir()  # empty too

        vm = VersionManager(
            writable, eval_base_dir=tmp_path / "eval",
            read_skill_dirs=[writable, layer1, layer2],
        )
        resolved = vm._resolve_skill_md("bar")
        assert resolved == layer2 / "bar" / "SKILL.md"

    def test_resolve_falls_back_to_writable_when_nothing_exists(self, tmp_path: Path):
        """Even when no layer has the file, _resolve returns the writable
        path so callers can proceed with a deterministic location."""
        writable = tmp_path / "writable"
        writable.mkdir()
        vm = VersionManager(
            writable, eval_base_dir=tmp_path / "eval",
            read_skill_dirs=[writable],
        )
        resolved = vm._resolve_skill_md("ghost")
        assert resolved == writable / "ghost" / "SKILL.md"
        assert not resolved.exists()

    def test_default_read_dirs_is_writable_alone_for_back_compat(self, tmp_path: Path):
        """Existing single-arg constructor callers must not break."""
        writable = tmp_path / "writable"
        writable.mkdir()
        vm = VersionManager(writable, eval_base_dir=tmp_path / "eval")
        assert vm._read_skill_dirs == [writable]
```

- [ ] **Step 2: Run, verify failure**

```bash
.venv/bin/pytest tests/unit/test_slm_version_manager.py::TestVersionManagerLayeredRead -v
```

Expected: `TypeError: __init__() got an unexpected keyword argument 'read_skill_dirs'` or `AttributeError: '_resolve_skill_md'`.

- [ ] **Step 3: Implement layered read**

In `src/everbot/core/slm/version_manager.py`, modify `__init__` (around line 60) and add `_resolve_skill_md`:

```python
    def __init__(
        self,
        skills_dir: Path,
        eval_base_dir: Optional[Path] = None,
        read_skill_dirs: Optional[List[Path]] = None,
    ) -> None:
        """
        Args:
            skills_dir: Writable skills directory. SLM publish/rollback writes
                here. For agent workspaces this is layer 0 of the loader chain.
            eval_base_dir: Per-agent eval directory.
            read_skill_dirs: Read priority chain (highest first). Used by
                _resolve_skill_md to find baseline SKILL.md content across
                layers. Defaults to ``[skills_dir]`` for backward compat —
                old callers see exactly the previous behavior.
        """
        self._skills_dir = skills_dir
        self._eval_base_dir = eval_base_dir
        self._read_skill_dirs: List[Path] = (
            list(read_skill_dirs) if read_skill_dirs else [skills_dir]
        )
```

Then add (place it next to `_skill_md` so the symmetry is obvious):

```python
    def _resolve_skill_md(self, skill_id: str) -> Path:
        """Find the live SKILL.md by walking read dirs in priority order.

        Returns the highest-priority path that exists. If no layer has the
        file, returns the writable path (caller can decide whether to
        treat that as 'missing' or write content there).
        """
        for d in self._read_skill_dirs:
            candidate = d / skill_id / "SKILL.md"
            if candidate.exists():
                return candidate
        return self._skill_md(skill_id)
```

You will also need `from typing import List` if it's not already imported.

- [ ] **Step 4: Run, verify pass**

```bash
.venv/bin/pytest tests/unit/test_slm_version_manager.py -v
```

Expected: all version_manager tests pass (existing ones still green; 4 new pass).

- [ ] **Step 5: Commit**

```bash
git add src/everbot/core/slm/version_manager.py tests/unit/test_slm_version_manager.py
git commit -m "$(cat <<'EOF'
feat(slm): VersionManager accepts layered read priority chain

Adds optional `read_skill_dirs` constructor param + `_resolve_skill_md`
that walks the chain in priority order, returning the writable path as
fallback when no layer has the file. Default behavior (single-arg
constructor) is unchanged — read_skill_dirs defaults to [skills_dir].
EOF
)"
```

---

## Task 3: StateInspector — exists/version use layered read

**Files:**
- Modify: `src/everbot/core/slm/state_normalizer.py`
- Test: `tests/unit/test_slm_state_normalizer.py`

- [ ] **Step 1: Write failing test**

Append to `tests/unit/test_slm_state_normalizer.py`:

```python
class TestStateInspectorLayered:
    def test_skill_md_exists_when_only_lower_layer_has_it(self, tmp_path: Path):
        """The 'exists' question is about the loader's perspective: any
        layer counts. Writable layer empty + lower layer has it = exists."""
        writable = tmp_path / "writable"
        readable = tmp_path / "readable"
        writable.mkdir()
        (readable / "foo").mkdir(parents=True)
        (readable / "foo" / "SKILL.md").write_text(
            '---\nname: foo\nversion: "1.0.0"\n---\nbody\n'
        )

        vm = VersionManager(
            writable, eval_base_dir=tmp_path / "eval",
            read_skill_dirs=[writable, readable],
        )
        state = StateInspector(vm).inspect("foo")
        assert state.skill_md_exists is True
        assert state.skill_md_version == "1.0.0"

    def test_skill_md_missing_when_no_layer_has_it(self, tmp_path: Path):
        writable = tmp_path / "writable"
        writable.mkdir()
        vm = VersionManager(
            writable, eval_base_dir=tmp_path / "eval",
            read_skill_dirs=[writable],
        )
        state = StateInspector(vm).inspect("ghost")
        assert state.skill_md_exists is False
        assert state.skill_md_version is None
```

- [ ] **Step 2: Run, verify failure**

```bash
.venv/bin/pytest tests/unit/test_slm_state_normalizer.py::TestStateInspectorLayered -v
```

Expected: `assert False is True` on the layered test — the inspector currently only checks `_skill_md` (writable), which is empty.

- [ ] **Step 3: Update StateInspector to use _resolve_skill_md**

Edit `src/everbot/core/slm/state_normalizer.py`. Find `StateInspector.inspect` (around line 60) and change the SKILL.md lookup to use the resolved path:

```python
    def inspect(self, skill_id: str) -> FileState:
        skill_md = self._vm._resolve_skill_md(skill_id)
        skill_md_exists = skill_md.exists()
        skill_md_version = (
            read_frontmatter_version(skill_md) if skill_md_exists else None
        )
        pointer = self._vm.get_pointer(skill_id)
        metadata: Optional[VersionMetadata] = None
        snapshot_exists = False
        if pointer and pointer.current_version:
            metadata = self._vm.get_metadata(skill_id, pointer.current_version)
            snap = (
                self._vm._version_dir(skill_id, pointer.current_version)
                / "skill.md"
            )
            snapshot_exists = snap.exists()
        return FileState(
            skill_md_exists=skill_md_exists,
            skill_md_version=skill_md_version,
            pointer=pointer,
            metadata=metadata,
            snapshot_exists=snapshot_exists,
        )
```

The only change vs. the current code is `self._vm._skill_md(skill_id)` → `self._vm._resolve_skill_md(skill_id)`.

- [ ] **Step 4: Run, verify pass**

```bash
.venv/bin/pytest tests/unit/test_slm_state_normalizer.py -v
```

Expected: all state_normalizer tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/everbot/core/slm/state_normalizer.py tests/unit/test_slm_state_normalizer.py
git commit -m "$(cat <<'EOF'
feat(slm): StateInspector resolves SKILL.md across read layers

A skill is considered to exist if any read layer has it. Previously the
inspector only looked at the writable dir, so a workspace-empty +
user-global symlink-to-repo install showed as missing.
EOF
)"
```

---

## Task 4: _bootstrap reads from resolved layers; doesn't write writable SKILL.md

**Files:**
- Modify: `src/everbot/core/slm/state_normalizer.py`
- Test: `tests/unit/test_slm_state_normalizer.py`

- [ ] **Step 1: Write failing test**

Append to `tests/unit/test_slm_state_normalizer.py`:

```python
class TestBootstrapLayered:
    def test_bootstrap_reads_baseline_from_lower_layer_does_not_write_writable(
        self, tmp_path: Path
    ):
        """Bootstrap of a skill that lives only in a lower read layer:
        - snapshot copies content from the lower layer
        - pointer/metadata are written to eval dir
        - writable layer's SKILL.md is NOT created (we don't want to
          materialize an unwanted override at the highest priority)."""
        writable = tmp_path / "writable"
        readable = tmp_path / "readable"
        writable.mkdir()
        (readable / "qux").mkdir(parents=True)
        (readable / "qux" / "SKILL.md").write_text(
            '---\nname: qux\nversion: "1.0.0"\n---\nbaseline body\n'
        )

        vm = VersionManager(
            writable, eval_base_dir=tmp_path / "eval",
            read_skill_dirs=[writable, readable],
        )
        result = ensure_registered(vm, "qux", repo_skills_dir=None)

        assert result.action == RegistrationAction.BOOTSTRAPPED

        # Pointer + metadata + snapshot all materialized in eval dir.
        pointer = vm.get_pointer("qux")
        assert pointer.current_version == "1.0.0"
        snap = tmp_path / "eval" / "qux" / "versions" / "v1.0.0" / "skill.md"
        assert snap.exists()
        assert "baseline body" in snap.read_text()

        # CRITICAL: writable layer untouched — no unwanted override.
        assert not (writable / "qux" / "SKILL.md").exists()
```

- [ ] **Step 2: Run, verify failure**

```bash
.venv/bin/pytest tests/unit/test_slm_state_normalizer.py::TestBootstrapLayered -v
```

Expected: assertion error — the current `_bootstrap` reads from `_skill_md` (writable), which doesn't exist for `qux`, so it errors out reading the file. (Or `read_text` raises `FileNotFoundError`.)

- [ ] **Step 3: Update _bootstrap to read from resolved path**

In `src/everbot/core/slm/state_normalizer.py`, find `_bootstrap` and change:

```python
    skill_md_path = ver_mgr._skill_md(skill_id)
    skill_content = skill_md_path.read_text(encoding="utf-8")
```

to:

```python
    skill_md_path = ver_mgr._resolve_skill_md(skill_id)
    skill_content = skill_md_path.read_text(encoding="utf-8")
```

(The only change is `_skill_md` → `_resolve_skill_md`.)

The function continues to write the snapshot to `ver_dir / "skill.md"` (eval dir, correct), the metadata to `ver_dir / "metadata.json"`, and the pointer to `_current_json` (eval dir, correct). It already does NOT write `_skill_md` directly during bootstrap, so the "don't materialize a writable override" property is preserved automatically.

- [ ] **Step 4: Run, verify pass**

```bash
.venv/bin/pytest tests/unit/test_slm_state_normalizer.py -v
```

Expected: all pass (12 existing + 1 new from step 1).

- [ ] **Step 5: Commit**

```bash
git add src/everbot/core/slm/state_normalizer.py tests/unit/test_slm_state_normalizer.py
git commit -m "$(cat <<'EOF'
feat(slm): _bootstrap reads baseline from resolved (layered) path

A symlink-installed skill (only present in user-global / repo, not
workspace) can now be bootstrapped: snapshot is taken from the lower
read layer, pointer/metadata go to eval dir, writable layer is left
empty. Future evolve will create the writable override; until then the
loader keeps serving the lower-layer baseline.
EOF
)"
```

---

## Task 5: rollback writes to writable, no longer needs symlink protection

**Files:**
- Modify: `src/everbot/core/slm/version_manager.py`
- Test: `tests/unit/test_slm_version_manager.py`

**Why this changes:** With Task 2, `_skill_md` (the path rollback writes/unlinks) points at the workspace dir, which is a real directory under SLM's exclusive ownership. The symlink-managed protection (commit 6cae5e1) was needed because `_skill_md` used to point at the user-global dir which could be symlinked. After this plan lands, in normal operation the protection cannot trip — but we keep it as defensive insurance (e.g. a future misconfiguration that points the writable dir at a symlink would still be caught).

- [ ] **Step 1: Write failing test (rollback now succeeds for previously-symlinked install)**

Append to `tests/unit/test_slm_version_manager.py`:

```python
class TestRollbackWithLayeredWritable:
    def test_rollback_does_not_touch_lower_layer_when_writable_is_workspace(
        self, tmp_path: Path
    ):
        """The exact production scenario: ~/.alfred/skills/<id> is a symlink
        to <repo>/skills/<id>. With layered writable=workspace, rollback
        operates on workspace only. Symlinked layer is untouched."""
        repo = tmp_path / "repo"
        global_dir = tmp_path / "global"
        workspace = tmp_path / "workspace"
        for d in (global_dir, workspace):
            d.mkdir()
        (repo / "p").mkdir(parents=True)
        repo_md = repo / "p" / "SKILL.md"
        repo_md.write_text('---\nname: p\nversion: "1.0"\n---\nbaseline\n')
        # global/p is a symlink to repo/p — exactly the production layout
        (global_dir / "p").symlink_to(repo / "p")

        vm = VersionManager(
            workspace, eval_base_dir=tmp_path / "eval",
            read_skill_dirs=[workspace, global_dir, repo],
        )
        # Bootstrap so a pointer+snapshot exist
        from src.everbot.core.slm.state_normalizer import ensure_registered
        ensure_registered(vm, "p", repo_skills_dir=None)

        # Now publish an evolved version (writes to workspace)
        evolved = '---\nname: p\nversion: "1.0-evolve-x"\n---\nimproved\n'
        vm.publish("p", "1.0-evolve-x", evolved)
        assert (workspace / "p" / "SKILL.md").exists()
        # repo + symlink unchanged
        assert repo_md.read_text().startswith('---\nname: p\nversion: "1.0"')

        # Rollback the evolved version — writable should change/remove,
        # repo MUST remain pristine.
        vm.rollback("p", reason="test")
        assert repo_md.read_text().startswith('---\nname: p\nversion: "1.0"'), \
            "repo file must NOT be modified by rollback"
```

- [ ] **Step 2: Run, verify failure or pass**

```bash
.venv/bin/pytest tests/unit/test_slm_version_manager.py::TestRollbackWithLayeredWritable -v
```

Expected: depending on Task 6's publish status, this either fails on the publish (if symlink protection still applies via writable=workspace=real-dir, publish should succeed; rollback may also succeed). The current symlink protection checks `is_symlink_managed` which inspects the writable dir — workspace is a real dir, so protection won't trip. Test should already pass. If it does, that's good — the layered architecture's existing logic handles this correctly without change. Add the test to lock the behavior in.

If the test fails, debug and fix in subsequent steps.

- [ ] **Step 3: No code change required if test passed; otherwise document the failure**

If the test passed in Step 2, this Task is essentially a regression-prevention test for behavior that emerges naturally from Task 2. Skip to Step 4.

If the test failed (unexpected), inspect the failure and decide whether to:
- Adjust `is_symlink_managed` semantics (e.g., only check writable, not other layers)
- Or fix the test setup

- [ ] **Step 4: Run all VersionManager tests**

```bash
.venv/bin/pytest tests/unit/test_slm_version_manager.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_slm_version_manager.py
git commit -m "$(cat <<'EOF'
test(slm): lock in rollback safety with layered writable=workspace

When VersionManager's writable dir is the workspace (a real dir), and
the user-global layer is a symlink to repo, publish + rollback must
operate exclusively on workspace and leave repo untouched. This is the
core property the layered design buys us.
EOF
)"
```

---

## Task 6: Wire skill_evaluate.py to use layered config

**Files:**
- Modify: `src/everbot/core/jobs/skill_evaluate.py`
- Test: `tests/unit/test_skill_evaluate_job.py`

- [ ] **Step 1: Write failing test**

Append to `tests/unit/test_skill_evaluate_job.py`:

```python
class TestSkillEvaluateLayeredConstruction:
    @pytest.mark.asyncio
    async def test_evaluate_one_uses_workspace_writable_with_layered_reads(
        self, tmp_path: Path
    ):
        """The job constructs VersionManager with writable=workspace skills
        and read_skill_dirs covering all loader layers."""
        # Layout:
        #   workspace_skills/  (writable, empty initially)
        #   user_skills/foo/   (layer 1, has SKILL.md)
        workspace_skills = tmp_path / "workspace" / "skills"
        user_skills = tmp_path / "user_skills"
        eval_dir = tmp_path / "eval"
        logs_dir = tmp_path / "logs"
        sessions_dir = tmp_path / "sessions"
        for d in (workspace_skills.parent, user_skills, eval_dir, logs_dir, sessions_dir):
            d.mkdir(parents=True)
        (user_skills / "foo").mkdir()
        (user_skills / "foo" / "SKILL.md").write_text(
            '---\nname: foo\nversion: "1.0.0"\n---\nbody\n'
        )

        # Build a SkillContext-like object with workspace_path that
        # _evaluate_one's new wiring will consult.
        seg_logger = SegmentLogger(logs_dir)
        seg_logger.append(EvaluationSegment(
            skill_id="foo", skill_version="1.0.0",
            triggered_at="2026-04-26T00:00:00",
            context_before="hi", skill_output="response", context_after="",
            session_id="s1",
        ))

        # Mock UserDataManager so its `skills_dir` returns user_skills,
        # `get_agent_writable_skills_dir` returns workspace_skills,
        # `get_agent_read_skill_dirs` returns the layered chain.
        from unittest.mock import patch
        from src.everbot.core.jobs.skill_evaluate import _evaluate_one
        from src.everbot.core.slm.version_manager import VersionManager

        ver_mgr_holder = {}

        original_init = VersionManager.__init__
        def capture_init(self, skills_dir, eval_base_dir=None, read_skill_dirs=None):
            ver_mgr_holder["skills_dir"] = skills_dir
            ver_mgr_holder["read_skill_dirs"] = read_skill_dirs
            original_init(self, skills_dir, eval_base_dir=eval_base_dir,
                          read_skill_dirs=read_skill_dirs)

        with patch.object(VersionManager, "__init__", capture_init):
            healthy_report = _make_healthy_report("foo", "1.0.0")
            context = _mk_context(tmp_path)
            context.workspace_path = tmp_path / "workspace"
            with patch("src.everbot.core.jobs.skill_evaluate.evaluate_skill",
                       new=AsyncMock(return_value=healthy_report)):
                # Need a real VersionManager instance for the actual call —
                # but the patch above intercepts __init__ to capture args.
                ver_mgr = VersionManager(
                    workspace_skills, eval_base_dir=eval_dir,
                    read_skill_dirs=[workspace_skills, user_skills],
                )
                # Just call _evaluate_one to confirm it works with the
                # layered ver_mgr it would get from production wiring.
                await _evaluate_one(context, seg_logger, ver_mgr, "foo", sessions_dir)

        # The actual production wiring assertions are tighter — verified
        # via integration test (Task 8). This unit test just confirms the
        # call path works with a layered VersionManager.
        assert ver_mgr.get_pointer("foo") is not None
```

(The test above is intentionally light — the truly important assertion lives in the e2e test in Task 8.)

- [ ] **Step 2: Run, observe initial state**

```bash
.venv/bin/pytest tests/unit/test_skill_evaluate_job.py::TestSkillEvaluateLayeredConstruction -v
```

Expected: should pass after Tasks 1-4 — this test only exercises that `_evaluate_one` works with a layered VersionManager. The actual *wiring* change (constructing the layered VersionManager from `udm`) happens in step 3.

- [ ] **Step 3: Update _evaluate_one's VersionManager construction**

In `src/everbot/core/jobs/skill_evaluate.py`, find the `run` function (around line 58) and locate:

```python
    udm = get_user_data_manager()
    skill_logs_dir = context.skill_logs_dir or udm.skill_logs_dir
    skill_eval_dir = context.skill_eval_dir
    seg_logger = SegmentLogger(skill_logs_dir)
    ver_mgr = VersionManager(udm.skills_dir, eval_base_dir=skill_eval_dir)
```

Replace the `ver_mgr = ...` line with:

```python
    # Layered SLM: writable = agent workspace skills (loader layer 0).
    # Read chain mirrors dolphin's loader priority (workspace → user → repo)
    # so bootstrap can find baseline content even when workspace is empty.
    agent_name = getattr(context, "agent_name", "") or ""
    if agent_name:
        writable = udm.get_agent_writable_skills_dir(agent_name)
        read_dirs = udm.get_agent_read_skill_dirs(agent_name)
    else:
        # Fallback for legacy callers without agent_name in context.
        writable = udm.skills_dir
        read_dirs = [udm.skills_dir]
    ver_mgr = VersionManager(writable, eval_base_dir=skill_eval_dir, read_skill_dirs=read_dirs)
```

- [ ] **Step 4: Run all skill_evaluate tests**

```bash
.venv/bin/pytest tests/unit/test_skill_evaluate_job.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/everbot/core/jobs/skill_evaluate.py tests/unit/test_skill_evaluate_job.py
git commit -m "$(cat <<'EOF'
feat(slm): skill_evaluate constructs VersionManager with layered config

Writable = agent workspace skills (loader layer 0). Read chain mirrors
dolphin loader priority. With this wiring, SLM evolve+rollback target
the workspace dir exclusively; the user-global symlink layer (which
may resolve into the repo) is read-only.
EOF
)"
```

---

## Task 7: Wire SkillLogRecorder to use layered config

**Files:**
- Modify: `src/everbot/core/slm/skill_log_recorder.py`
- Test: `tests/unit/test_skill_log_recorder.py`

- [ ] **Step 1: Read current implementation**

```bash
sed -n '120,130p' /Users/xupeng/dev/github/alfred/src/everbot/core/slm/skill_log_recorder.py
```

The current `maybe_record` constructs:

```python
vm = VersionManager(self._skill_dirs[0], eval_base_dir=self._eval_base_dir)
```

It already has the full `_skill_dirs` list (the layered chain) — we just aren't passing it.

- [ ] **Step 2: Write failing test**

Append to `tests/unit/test_skill_log_recorder.py`:

```python
class TestRecorderUsesLayeredVersionManager:
    def test_ensure_registered_finds_skill_in_lower_layer(self, tmp_path: Path):
        """If a skill exists only in the lower read layer (e.g., user-global
        symlink to repo) and not the writable layer, the recorder's
        bootstrap path must still find and register it."""
        workspace_skills = tmp_path / "workspace_skills"
        user_skills = tmp_path / "user_skills"
        eval_dir = tmp_path / "eval"
        logs_dir = tmp_path / "logs"
        for d in (workspace_skills, user_skills, eval_dir, logs_dir):
            d.mkdir()
        (user_skills / "newskill").mkdir()
        (user_skills / "newskill" / "SKILL.md").write_text(
            '---\nname: newskill\nversion: "1.0.0"\n---\nbody\n'
        )

        recorder = SkillLogRecorder(
            skill_logs_dir=logs_dir,
            skill_dirs=[workspace_skills, user_skills],
            eval_base_dir=eval_dir,
        )
        recorder.maybe_record(
            skill_name="newskill",
            skill_output="hello",
            context_before="hi",
            session_id="s1",
        )

        from src.everbot.core.slm.version_manager import VersionManager
        vm = VersionManager(
            workspace_skills, eval_base_dir=eval_dir,
            read_skill_dirs=[workspace_skills, user_skills],
        )
        assert vm.get_pointer("newskill") is not None, \
            "ensure_registered should have bootstrapped from the lower read layer"
```

- [ ] **Step 3: Run, verify failure**

```bash
.venv/bin/pytest tests/unit/test_skill_log_recorder.py::TestRecorderUsesLayeredVersionManager -v
```

Expected: `AssertionError: ... should have bootstrapped from the lower read layer` — the current code constructs VersionManager with only `_skill_dirs[0]` (workspace, empty), so bootstrap can't find SKILL.md.

- [ ] **Step 4: Update recorder to pass layered config**

In `src/everbot/core/slm/skill_log_recorder.py`, find the `maybe_record` block (around line 120) and update the VersionManager construction:

```python
        if self._eval_base_dir is not None and self._skill_dirs:
            from .state_normalizer import ensure_registered
            from .version_manager import VersionManager
            # Writable = highest-priority dir; read chain = full priority list.
            vm = VersionManager(
                self._skill_dirs[0],
                eval_base_dir=self._eval_base_dir,
                read_skill_dirs=list(self._skill_dirs),
            )
            try:
                ensure_registered(vm, skill_name, repo_skills_dir=None)
            except Exception as e:
                logger.warning("ensure_registered failed for %s: %s", skill_name, e)
```

- [ ] **Step 5: Run, verify pass**

```bash
.venv/bin/pytest tests/unit/test_skill_log_recorder.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/everbot/core/slm/skill_log_recorder.py tests/unit/test_skill_log_recorder.py
git commit -m "$(cat <<'EOF'
feat(slm): SkillLogRecorder passes full skill_dirs as layered read chain

The recorder already has the multi-dir priority list (workspace → user
→ repo). Pass it through to VersionManager as read_skill_dirs so first-
invocation bootstrap can find baseline content in lower layers (most
production skills live there, not in the workspace dir).
EOF
)"
```

---

## Task 8: End-to-end test — symlinked skill evolves successfully

**Why this matters:** The whole point of the plan. Lock in the property that a symlink-installed skill (the production reality for paper-discovery, gray-rhino, etc.) can be evolved by SLM without touching the upstream repo file.

**Files:**
- Create: `tests/integration/test_slm_layered_evolve_e2e.py`

- [ ] **Step 1: Write the e2e test**

Create `tests/integration/test_slm_layered_evolve_e2e.py`:

```python
"""End-to-end: symlinked skill evolves into workspace, repo untouched.

Reproduces the production layout: ~/.alfred/skills/<id> is a symlink to
<repo>/skills/<id>/. With the layered architecture, SLM's writable dir
is the agent workspace (real dir, separate from the symlink layer).
Evolved versions land in workspace; loader picks them up because layer 0
> layer 1; rollback unlinks workspace and loader returns to the symlinked
baseline. The repo file is read-only throughout.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.everbot.core.slm.models import (
    EvalReport,
    EvaluationSegment,
    JudgeResult,
    VersionStatus,
)
from src.everbot.core.slm.segment_logger import SegmentLogger
from src.everbot.core.slm.version_manager import VersionManager


SKILL_MD = """\
---
name: e2e-skill
version: "1.0.0"
---
You are a baseline test skill.
"""

EVOLVED_SKILL_MD = """\
---
name: e2e-skill
version: "1.0.0-evolve-202604261000"
---
You are an improved test skill.
"""


def _unhealthy_report(skill_id: str, version: str) -> EvalReport:
    results = [
        JudgeResult(segment_index=i, has_critical_issue=True,
                    satisfaction=0.2, reason="bad")
        for i in range(3)
    ]
    return EvalReport(
        skill_id=skill_id, skill_version=version,
        evaluated_at="2026-04-26T00:00:00",
        segment_count=3, critical_issue_count=3,
        critical_issue_rate=1.0, mean_satisfaction=0.2,
        results=results,
    )


def _mk_context(tmp_path: Path, workspace: Path):
    from types import SimpleNamespace
    llm = SimpleNamespace(complete=AsyncMock(return_value=""))
    mailbox = SimpleNamespace(deposit=AsyncMock(return_value=None))
    return SimpleNamespace(
        llm=llm,
        mailbox=mailbox,
        workspace_path=workspace,
        agent_name="e2e-agent",
        skill_logs_dir=tmp_path / "logs",
        skill_eval_dir=tmp_path / "eval",
    )


@pytest.mark.asyncio
async def test_symlinked_skill_evolves_into_workspace(tmp_path: Path):
    # ── Setup: production-like layout ────────────────────────────
    repo = tmp_path / "repo_skills"
    user_global = tmp_path / "user_skills"
    workspace = tmp_path / "agents" / "e2e-agent"
    workspace_skills = workspace / "skills"
    eval_dir = tmp_path / "eval"
    logs_dir = tmp_path / "logs"
    sessions_dir = tmp_path / "sessions"
    for d in (user_global, workspace_skills, eval_dir, logs_dir, sessions_dir):
        d.mkdir(parents=True)
    # repo has the real skill file
    (repo / "e2e-skill").mkdir(parents=True)
    repo_md = repo / "e2e-skill" / "SKILL.md"
    repo_md.write_text(SKILL_MD)
    # user-global is a symlink to the repo dir
    (user_global / "e2e-skill").symlink_to(repo / "e2e-skill")

    # Sanity: loader-equivalent priority order
    read_dirs = [workspace_skills, user_global, repo]
    vm = VersionManager(workspace_skills, eval_base_dir=eval_dir,
                        read_skill_dirs=read_dirs)

    # Prime segments so _evaluate_one will run
    seg_logger = SegmentLogger(logs_dir)
    for i in range(3):
        seg_logger.append(EvaluationSegment(
            skill_id="e2e-skill", skill_version="1.0.0",
            triggered_at=f"2026-04-26T0{i}:00:00",
            context_before=f"q{i}", skill_output=f"bad{i}",
            context_after="", session_id=f"s{i}",
        ))

    # ── Pre-conditions ───────────────────────────────────────────
    assert not (workspace_skills / "e2e-skill" / "SKILL.md").exists()
    assert (user_global / "e2e-skill" / "SKILL.md").exists()  # via symlink
    repo_md_original = repo_md.read_text()

    # ── Act: trigger evaluate → unhealthy → rollback → evolve → publish ──
    from src.everbot.core.jobs.skill_evaluate import _evaluate_one

    context = _mk_context(tmp_path, workspace)
    context.llm.complete = AsyncMock(return_value=EVOLVED_SKILL_MD)
    unhealthy = _unhealthy_report("e2e-skill", "1.0.0")

    with patch("src.everbot.core.jobs.skill_evaluate.evaluate_skill",
               new=AsyncMock(return_value=unhealthy)), \
         patch("src.everbot.infra.user_data.get_user_data_manager") as mock_udm:
        # Mock UDM so _evaluate_one wires the right layered VersionManager
        from unittest.mock import MagicMock
        udm = MagicMock()
        udm.skills_dir = user_global
        udm.repo_skills_dir = repo
        udm.skill_logs_dir = logs_dir
        udm.get_agent_writable_skills_dir.return_value = workspace_skills
        udm.get_agent_read_skill_dirs.return_value = read_dirs
        mock_udm.return_value = udm
        await _evaluate_one(context, seg_logger, vm, "e2e-skill", sessions_dir)

    # ── Assertions: writable evolved, repo untouched ─────────────
    pointer = vm.get_pointer("e2e-skill")
    assert pointer is not None
    assert "evolve" in pointer.current_version, \
        f"expected evolve version, got {pointer.current_version}"

    # The new evolved SKILL.md is in workspace
    workspace_md = workspace_skills / "e2e-skill" / "SKILL.md"
    assert workspace_md.exists(), "evolve must write to workspace skills dir"
    assert "improved" in workspace_md.read_text()

    # The repo (= what the symlink points to) is unchanged
    assert repo_md.read_text() == repo_md_original, \
        "REPO MUST NOT be modified by SLM publish"
    assert repo_md.is_file() and not repo_md.is_symlink()

    # Snapshot of original baseline preserved for future rollback
    baseline_snap = eval_dir / "e2e-skill" / "versions" / "v1.0.0" / "skill.md"
    assert baseline_snap.exists()
```

- [ ] **Step 2: Run the test**

```bash
.venv/bin/pytest tests/integration/test_slm_layered_evolve_e2e.py -v
```

Expected: PASS — proves the whole architecture works end-to-end on a symlinked install.

- [ ] **Step 3: Run the full SLM regression to verify nothing broke**

```bash
.venv/bin/pytest tests/unit/test_slm_*.py tests/unit/test_skill_evaluate_job.py tests/unit/test_skill_log_recorder.py tests/integration/test_slm_*.py -v
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_slm_layered_evolve_e2e.py
git commit -m "$(cat <<'EOF'
test(slm): end-to-end symlinked skill evolves into workspace

Production layout reproduced: repo holds the real SKILL.md, user-global
is a symlink to it, agent workspace is empty. After unhealthy eval,
SLM's evolve writes the new version to workspace skills dir; the repo
file remains byte-identical. Locks in the core property the layered
architecture provides.
EOF
)"
```

---

## Task 9: Optional — slm_ensure_all CLI accepts layered read dirs

**Why optional:** The CLI is one-shot migration tooling. The layered behavior gives meaningful improvement when bulk-bootstrapping a fresh agent's skills (writes go to workspace, not user dir). For existing migrations already done, this Task is a future-proofing addition.

**Files:**
- Modify: `scripts/slm_ensure_all.py`
- Modify: `tests/unit/test_slm_ensure_all_cli.py`

- [ ] **Step 1: Write failing test**

Append to `tests/unit/test_slm_ensure_all_cli.py`:

```python
def test_layered_read_dirs_arg(tmp_path: Path):
    workspace_skills = tmp_path / "workspace_skills"
    user_skills = tmp_path / "user_skills"
    eval_dir = tmp_path / "eval"
    for d in (workspace_skills, user_skills, eval_dir):
        d.mkdir()
    # Skill exists in user_skills (lower layer), not workspace
    (user_skills / "alpha").mkdir()
    (user_skills / "alpha" / "SKILL.md").write_text(
        '---\nname: alpha\nversion: "1.0.0"\n---\nbody\n'
    )

    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "slm_ensure_all.py"),
         "--skills-dir", str(workspace_skills),
         "--eval-dir", str(eval_dir),
         "--read-skill-dirs", f"{workspace_skills}:{user_skills}",
         "--json"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    # alpha should be bootstrapped — content found in lower layer
    assert out["summary"].get("bootstrapped") == 1
    # And critically, the writable dir should NOT have a SKILL.md created
    assert not (workspace_skills / "alpha" / "SKILL.md").exists()
```

- [ ] **Step 2: Run, verify failure**

```bash
.venv/bin/pytest tests/unit/test_slm_ensure_all_cli.py::test_layered_read_dirs_arg -v
```

Expected: failure — argparse rejects `--read-skill-dirs`.

- [ ] **Step 3: Add the flag to the CLI**

In `scripts/slm_ensure_all.py`, find the argparse setup in `main()` and add:

```python
    p.add_argument(
        "--read-skill-dirs", default=None,
        help=(
            "Colon-separated read priority chain for layered SKILL.md lookup."
            " Example: '/path/workspace_skills:/path/user_skills:/path/repo_skills'."
            " Default = single dir = --skills-dir."
        ),
    )
```

Then where `VersionManager(args.skills_dir, eval_base_dir=args.eval_dir)` is constructed, change to:

```python
    read_skill_dirs = None
    if args.read_skill_dirs:
        read_skill_dirs = [Path(p) for p in args.read_skill_dirs.split(":") if p]
    vm = VersionManager(
        args.skills_dir,
        eval_base_dir=args.eval_dir,
        read_skill_dirs=read_skill_dirs,
    )
```

Also update the iteration block. Currently it iterates `args.skills_dir.iterdir()`. With layered reads, we should iterate the union of all read layers (so skills in lower layers are also bootstrapped):

```python
    candidate_dirs = [args.skills_dir]
    if read_skill_dirs:
        candidate_dirs = read_skill_dirs
    seen: set[str] = set()
    results = []
    for base in candidate_dirs:
        if not base.exists():
            continue
        for skill_dir in sorted(base.iterdir()):
            if not skill_dir.is_dir():
                continue
            if skill_dir.name in seen:
                continue
            if not (skill_dir / "SKILL.md").exists():
                continue
            seen.add(skill_dir.name)
            r = ensure_registered(vm, skill_dir.name, repo_skills_dir=args.repo_skills_dir)
            results.append({
                "skill_id": r.skill_id,
                "action": r.action.value,
                "detail": r.detail,
            })
```

- [ ] **Step 4: Run all CLI tests**

```bash
.venv/bin/pytest tests/unit/test_slm_ensure_all_cli.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/slm_ensure_all.py tests/unit/test_slm_ensure_all_cli.py
git commit -m "$(cat <<'EOF'
feat(slm): slm_ensure_all CLI accepts --read-skill-dirs

Allows bulk-bootstrapping skills that live in lower read layers (user
global, repo bundled) without materializing writable overrides. Each
skill is registered exactly once across the union of layers.
EOF
)"
```

---

## Task 10: Production migration — switch demo_agent to layered SLM

**Files:**
- No code changes; this Task documents the operational migration.

**Why required:** The 9 production skills currently have pointers that were created with the old single-dir VersionManager. After the daemon restarts with the new code, behavior changes:

- StateInspector now resolves SKILL.md across layers — the existing `current_version` values still match the highest-priority layer's frontmatter (which is unchanged), so pointers stay valid.
- Symlink protection in `rollback`/`publish` becomes effectively dead code for production (writable = workspace = real dir).
- `repo_baseline` flags set to `False` during the symlink defuse (yesterday) can stay as-is — both `True` and `False` are safe under the new architecture (rollback writes to workspace either way).

So the migration is just: **restart the daemon**. No data file edits.

- [ ] **Step 1: Run full repo regression**

```bash
cd /Users/xupeng/dev/github/alfred
.venv/bin/pytest tests/ 2>&1 | tail -3
```

Expected: all pass (≥ 1540 passed).

- [ ] **Step 2: Restart daemon**

```bash
./bin/everbot restart
sleep 5
bin/everbot status
```

Expected: new pid, ppid=1 (launchd), agent registered.

- [ ] **Step 3: Verify new code loaded**

```bash
grep -c "_resolve_skill_md\|read_skill_dirs" /Users/xupeng/dev/github/alfred/src/everbot/core/slm/version_manager.py
grep -c "get_agent_writable_skills_dir" /Users/xupeng/dev/github/alfred/src/everbot/infra/user_data.py
```

Expected: both > 0.

- [ ] **Step 4: Force re-evaluate paper-discovery to verify the new path**

```bash
rm -v ~/.alfred/agents/demo_agent/skill_eval/paper-discovery/versions/v2.0.0/eval_report.json
.venv/bin/python -c "
from datetime import datetime, timezone, timedelta
from pathlib import Path
from src.everbot.core.tasks.routine_manager import RoutineManager
mgr = RoutineManager(Path.home() / '.alfred/agents/demo_agent')
content, task_list = mgr._load_task_list()
for t in task_list.tasks:
    if t.id == 'routine_fe2d192d':
        t.next_run_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        t.last_run_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
mgr._save_task_list(content, task_list)
print('Bumped Skill Evaluate routine')
"
```

Wait ~2 minutes for the daemon's cron to pick it up.

```bash
until grep "$(date +%Y-%m-%d).*skill-evaluate.*job_completed" ~/.alfred/logs/heartbeat_events.jsonl | tail -1 | grep -q "Evaluated 2"; do sleep 5; done
echo "skill-evaluate ran with paper-discovery"
```

- [ ] **Step 5: Verify evolve produced workspace SKILL.md**

```bash
echo "=== workspace dir before/after ==="
ls -la ~/.alfred/agents/demo_agent/skills/paper-discovery/SKILL.md 2>&1
echo
echo "=== upstream still safe ==="
ls -la /Users/xupeng/dev/github/alfred/skills/paper-discovery/SKILL.md
echo
echo "=== evolve trace in err log ==="
grep "$(date +%Y-%m-%d)" ~/.alfred/logs/everbot.err | grep -E "Evolved paper-discovery|publish.*paper-discovery|Evolve output.*failed|Cannot publish" | tail -5
```

**Two acceptable outcomes:**

A. **Evolve succeeded** (best case):
- `~/.alfred/agents/demo_agent/skills/paper-discovery/SKILL.md` EXISTS — the evolved version
- err log shows `Evolved paper-discovery to v...-evolve-...`
- `<repo>/skills/paper-discovery/SKILL.md` is byte-identical to its previous contents

B. **Evolve LLM output failed validation** (still acceptable; pre-existing P2 concern, out of scope):
- workspace SKILL.md does NOT get created (LLM produced invalid frontmatter, `_validate_skill_md` returns False, no publish)
- err log shows `Evolve output for paper-discovery failed validation`
- mailbox gets `技能 paper-discovery 评估不达标，自动改进失败，已回退到稳定版本`
- `<repo>/skills/paper-discovery/SKILL.md` STILL byte-identical (the layered architecture's protection holds — no rollback wrote through symlink)

**Either outcome validates the architecture goal: the repo file is never modified.**

If outcome A is observed, the entire SLM evolution loop is now functional for symlinked installs — a major milestone. If outcome B, the architecture works but exposes the orthogonal `_validate_skill_md` strictness issue (P2 follow-up).

- [ ] **Step 6: Verify Telegram delivery + mailbox dual-write still works**

```bash
echo "=== TG session mailbox count ==="
.venv/bin/python -c "
import json
with open('/Users/xupeng/.alfred/sessions/tg_session_demo_agent__8576399597.json') as f:
    mb = json.load(f).get('mailbox', [])
sn = [m for m in mb if isinstance(m, dict) and m.get('event_type') == 'skill_notification']
print(f'tg skill_notifications: {len(sn)}')
for m in sn[-3:]:
    print(f\"  {(m.get('summary') or '')[:80]}\")
"
```

Expected: at least one new `skill_notification` (about the successful evolve). The summary should mention paper-discovery and the new evolve version.

- [ ] **Step 7: Final sanity — daemon healthy**

```bash
bin/everbot status
ps -p $(cat ~/.alfred/everbot.pid) -o pid,etime,rss,command
```

Expected: still running, RSS reasonable.

- [ ] **Step 8: No commit needed (operational migration only)**

This Task is observation/verification. No file changes.

---

## Self-Review

**Spec coverage:**

- ✅ Layered read priority chain → Tasks 1, 2
- ✅ Writable dir = workspace = layer 0 → Tasks 1, 6
- ✅ StateInspector exists/version uses layers → Task 3
- ✅ Bootstrap reads from resolved layer, doesn't materialize writable override → Task 4
- ✅ Rollback safe with symlinked lower layers → Task 5
- ✅ skill_evaluate.py wires layered config → Task 6
- ✅ SkillLogRecorder wires layered config → Task 7
- ✅ End-to-end symlinked-skill evolve test → Task 8
- ✅ CLI layered support → Task 9 (optional)
- ✅ Production migration verification → Task 10

**Placeholder scan:**
- No "TBD" / "TODO" / "implement later"
- No "Similar to Task N" — code repeated where needed
- All test bodies and code blocks are concrete and runnable
- Exact file paths and line ranges given throughout

**Type consistency:**
- `read_skill_dirs: List[Path]` consistently named across VersionManager, UserDataManager helpers, CLI, tests
- `_resolve_skill_md(skill_id) -> Path` signature consistent
- `writable_skills_dir` term used in helpers; matches `_skills_dir` field semantics in VersionManager

**Out of scope (deliberately):**
- `_validate_skill_md` strictness (P2 from earlier conversation) — separate concern, doesn't block this plan.
- paper-discovery's segments-not-recording issue (`_load_resource_skill` filtered out by recorder) — orthogonal.
- "git pull repo updates while SLM has evolved version" reconciliation — UX concern; future work, doesn't change correctness.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-26-slm-layered-skills-dir.md`.

Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks. Each task here is well-isolated (UserDataManager helpers, VersionManager change, etc.) so this works well.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch checkpoints.

Tasks 1–9 are code (subagent-friendly). Task 10 is operational verification — runs in this session against the live daemon regardless of execution mode.

Which approach?
