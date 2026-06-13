"""Tests for SLM VersionManager."""

import pytest
from pathlib import Path

from src.everbot.core.slm.models import (
    EvalReport,
    JudgeResult,
    VersionStatus,
)
from src.everbot.core.slm.version_manager import VersionManager, read_frontmatter_version


SKILL_CONTENT_V1 = """\
---
name: test-skill
version: "1.0"
description: Test skill
---
This is version 1.0
"""

SKILL_CONTENT_V2 = """\
---
name: test-skill
version: "2.0"
description: Test skill updated
---
This is version 2.0
"""


class TestReadFrontmatterVersion:
    def test_with_version(self, tmp_path):
        p = tmp_path / "SKILL.md"
        p.write_text(SKILL_CONTENT_V1)
        assert read_frontmatter_version(p) == "1.0"

    def test_without_version(self, tmp_path):
        p = tmp_path / "SKILL.md"
        p.write_text("---\nname: foo\n---\ncontent")
        assert read_frontmatter_version(p) == "baseline"

    def test_no_frontmatter(self, tmp_path):
        p = tmp_path / "SKILL.md"
        p.write_text("just content")
        assert read_frontmatter_version(p) == "baseline"

    def test_nonexistent(self, tmp_path):
        assert read_frontmatter_version(tmp_path / "nope.md") == "baseline"


class TestVersionManager:
    def _make_mgr(self, tmp_path) -> VersionManager:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        return VersionManager(skills_dir)

    def test_publish_creates_structure(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        mgr.publish("test-skill", "1.0", SKILL_CONTENT_V1)

        # SKILL.md written
        skill_md = tmp_path / "skills" / "test-skill" / "SKILL.md"
        assert skill_md.exists()
        assert "version: \"1.0\"" in skill_md.read_text()

        # Snapshot created
        snapshot = tmp_path / "skills" / "test-skill" / ".eval" / "versions" / "v1.0" / "skill.md"
        assert snapshot.exists()

        # Metadata created
        meta = mgr.get_metadata("test-skill", "1.0")
        assert meta is not None
        assert meta.status == VersionStatus.TESTING

        # Pointer created
        ptr = mgr.get_pointer("test-skill")
        assert ptr is not None
        assert ptr.current_version == "1.0"
        assert ptr.repo_baseline is True  # first version, no prior stable

    def test_publish_second_testing_version_does_not_promote_previous_testing(self, tmp_path):
        """Publishing a new TESTING version must not bless the previous one.

        Regression: user-directed evolve can publish several candidate
        versions in quick succession. A previous candidate is still TESTING
        until Skill Evaluate calls activate(); publish() must not turn it into
        ACTIVE or make it the rollback target just because another candidate
        was published.
        """
        mgr = self._make_mgr(tmp_path)
        mgr.publish("test-skill", "1.0", SKILL_CONTENT_V1)
        mgr.publish("test-skill", "2.0", SKILL_CONTENT_V2)

        ptr = mgr.get_pointer("test-skill")
        assert ptr.current_version == "2.0"
        assert ptr.stable_version == ""
        assert ptr.repo_baseline is True

        v1_meta = mgr.get_metadata("test-skill", "1.0")
        assert v1_meta.status == VersionStatus.TESTING

    def test_publish_preserves_existing_stable_version(self, tmp_path):
        """Only activate() advances stable; later publish() preserves it."""
        mgr = self._make_mgr(tmp_path)
        mgr.publish("test-skill", "1.0", SKILL_CONTENT_V1)
        mgr.activate("test-skill", "1.0")

        mgr.publish("test-skill", "2.0", SKILL_CONTENT_V2)

        ptr = mgr.get_pointer("test-skill")
        assert ptr.current_version == "2.0"
        assert ptr.stable_version == "1.0"
        assert ptr.repo_baseline is False

        v1_meta = mgr.get_metadata("test-skill", "1.0")
        assert v1_meta.status == VersionStatus.ACTIVE

        v2_meta = mgr.get_metadata("test-skill", "2.0")
        assert v2_meta.status == VersionStatus.TESTING

    def test_rollback_to_stable(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        mgr.publish("test-skill", "1.0", SKILL_CONTENT_V1)
        mgr.activate("test-skill", "1.0")
        mgr.publish("test-skill", "2.0", SKILL_CONTENT_V2)

        rolled = mgr.rollback("test-skill", reason="critical issues")
        assert rolled == "1.0"

        # SKILL.md should be v1.0 content
        skill_md = tmp_path / "skills" / "test-skill" / "SKILL.md"
        assert "version: \"1.0\"" in skill_md.read_text()

        # Pointer updated
        ptr = mgr.get_pointer("test-skill")
        assert ptr.current_version == "1.0"

        # v2.0 suspended
        meta = mgr.get_metadata("test-skill", "2.0")
        assert meta.status == VersionStatus.SUSPENDED
        assert "critical issues" in meta.suspended_reason

    def test_rollback_to_repo_baseline(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        mgr.publish("test-skill", "1.0", SKILL_CONTENT_V1)

        rolled = mgr.rollback("test-skill", reason="bad first version")
        assert rolled == "baseline"

        # SKILL.md should be deleted
        skill_md = tmp_path / "skills" / "test-skill" / "SKILL.md"
        assert not skill_md.exists()

    def test_list_versions(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        mgr.publish("test-skill", "1.0", SKILL_CONTENT_V1)
        mgr.publish("test-skill", "2.0", SKILL_CONTENT_V2)
        assert mgr.list_versions("test-skill") == ["1.0", "2.0"]
        assert mgr.list_versions("nonexistent") == []

    def test_activate(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        mgr.publish("test-skill", "1.0", SKILL_CONTENT_V1)
        mgr.activate("test-skill", "1.0")

        meta = mgr.get_metadata("test-skill", "1.0")
        assert meta.status == VersionStatus.ACTIVE

        ptr = mgr.get_pointer("test-skill")
        assert ptr.stable_version == "1.0"
        assert ptr.repo_baseline is False

    def test_save_eval_report(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        mgr.publish("test-skill", "1.0", SKILL_CONTENT_V1)

        report = EvalReport.build(
            "test-skill", "1.0",
            [JudgeResult(0, False, 0.9, "ok")],
        )
        mgr.save_eval_report("test-skill", "1.0", report)

        loaded = mgr.get_eval_report("test-skill", "1.0")
        assert loaded is not None
        assert loaded.mean_satisfaction == 0.9

        meta = mgr.get_metadata("test-skill", "1.0")
        assert meta.eval_summary["satisfaction_score"] == 0.9

    def test_check_consistency_ok(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        mgr.publish("test-skill", "1.0", SKILL_CONTENT_V1)
        assert mgr.check_consistency("test-skill") is True

    def test_check_consistency_fixes_mismatch(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        mgr.publish("test-skill", "1.0", SKILL_CONTENT_V1)

        # Manually tamper pointer
        ptr_path = tmp_path / "skills" / "test-skill" / ".eval" / "current.json"
        ptr_path.write_text('{"current_version": "9.9", "stable_version": "1.0", "repo_baseline": false}')

        assert mgr.check_consistency("test-skill") is False
        ptr = mgr.get_pointer("test-skill")
        assert ptr.current_version == "1.0"  # fixed to match SKILL.md

    def test_get_active_version(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        assert mgr.get_active_version("nonexistent") == "baseline"
        mgr.publish("test-skill", "1.0", SKILL_CONTENT_V1)
        assert mgr.get_active_version("test-skill") == "1.0"


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


class TestCheckConsistencyNoPointer:
    def test_no_pointer_triggers_bootstrap(self, tmp_path):
        (tmp_path / "skills" / "foo").mkdir(parents=True)
        (tmp_path / "skills" / "foo" / "SKILL.md").write_text(SKILL_CONTENT_V1)
        (tmp_path / "eval").mkdir()
        vm = VersionManager(tmp_path / "skills", eval_base_dir=tmp_path / "eval")

        # Before fix: returns True silently with no pointer created.
        # After fix: delegates to ensure_registered which bootstraps.
        ok = vm.check_consistency("foo")

        assert ok is True
        assert vm.get_pointer("foo") is not None

    def test_no_pointer_and_no_skill_md_still_returns_true(self, tmp_path):
        """SKILL_MISSING case: nothing to bootstrap, nothing to break."""
        (tmp_path / "eval").mkdir()
        vm = VersionManager(tmp_path / "skills", eval_base_dir=tmp_path / "eval")
        (tmp_path / "skills").mkdir()

        ok = vm.check_consistency("ghost")

        assert ok is True
        assert vm.get_pointer("ghost") is None


class TestSymlinkProtection:
    def _setup_symlinked_skill(self, tmp_path, skill_id="paper", version="1.0"):
        """Create the install pattern that bit production: a real skill in
        upstream/, with ~/.alfred/skills/<id>/ as a symlink to it."""
        upstream = tmp_path / "upstream" / skill_id
        upstream.mkdir(parents=True)
        upstream_md = upstream / "SKILL.md"
        upstream_md.write_text(
            f'---\nname: {skill_id}\nversion: "{version}"\n---\nbody\n'
        )

        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        # Symlink the skill dir (not just the SKILL.md) — matches production layout.
        (skills_dir / skill_id).symlink_to(upstream)

        eval_dir = tmp_path / "eval"
        eval_dir.mkdir()
        return VersionManager(skills_dir, eval_base_dir=eval_dir), upstream_md

    def test_is_symlink_managed_detects_symlinked_dir(self, tmp_path):
        vm, _ = self._setup_symlinked_skill(tmp_path)
        assert vm.is_symlink_managed("paper") is True

    def test_is_symlink_managed_false_for_real_dir(self, tmp_path):
        skills_dir = tmp_path / "skills"
        (skills_dir / "real").mkdir(parents=True)
        (skills_dir / "real" / "SKILL.md").write_text(SKILL_CONTENT_V1)
        (tmp_path / "eval").mkdir()
        vm = VersionManager(skills_dir, eval_base_dir=tmp_path / "eval")
        assert vm.is_symlink_managed("real") is False

    def test_rollback_refuses_symlinked_skill(self, tmp_path):
        vm, upstream_md = self._setup_symlinked_skill(tmp_path)
        # Bootstrap so a pointer exists
        from src.everbot.core.slm.state_normalizer import ensure_registered
        ensure_registered(vm, "paper", repo_skills_dir=None)

        with pytest.raises(ValueError, match="symlink-managed"):
            vm.rollback("paper", reason="test")

        # Critical: upstream file was NOT touched
        assert upstream_md.exists()
        assert "version" in upstream_md.read_text()

    def test_publish_refuses_symlinked_skill(self, tmp_path):
        vm, upstream_md = self._setup_symlinked_skill(tmp_path)
        original_content = upstream_md.read_text()

        with pytest.raises(ValueError, match="symlink-managed"):
            vm.publish("paper", "2.0", '---\nname: paper\nversion: "2.0"\n---\nnew\n')

        # Critical: upstream content unchanged
        assert upstream_md.read_text() == original_content


class TestBootstrapSymlinkAware:
    def test_bootstrap_forces_repo_baseline_false_on_symlink(self, tmp_path):
        """Even if repo_skills_dir contains the skill, a symlink-managed
        user dir must NOT get repo_baseline=True (would arm a rollback bomb)."""
        from src.everbot.core.slm.state_normalizer import (
            ensure_registered,
            RegistrationAction,
        )

        # Repo skills dir
        repo_skills = tmp_path / "repo_skills"
        (repo_skills / "foo").mkdir(parents=True)
        (repo_skills / "foo" / "SKILL.md").write_text(SKILL_CONTENT_V1)
        # User skills dir is a symlink to repo
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "foo").symlink_to(repo_skills / "foo")
        (tmp_path / "eval").mkdir()

        vm = VersionManager(skills_dir, eval_base_dir=tmp_path / "eval")
        result = ensure_registered(vm, "foo", repo_skills_dir=repo_skills)

        assert result.action == RegistrationAction.BOOTSTRAPPED
        pointer = vm.get_pointer("foo")
        assert pointer is not None
        # The bug: without symlink detection, this would be True.
        assert pointer.repo_baseline is False


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


class TestPublishPropagatesUpstreamAssets:
    """Regression: alice's invest broke at runtime because publish wrote
    only SKILL.md to the per-agent override, shadowing the upstream symlink
    that provided scripts/ and references/. The evolved SKILL.md still
    referenced ``$SKILL_DIR/scripts/tools.py``, which now resolved into an
    empty workspace dir.
    """

    def _setup_layered_skill_with_assets(self, tmp_path: Path):
        repo = tmp_path / "repo"
        global_dir = tmp_path / "global"
        workspace = tmp_path / "workspace"
        for d in (global_dir, workspace):
            d.mkdir()
        (repo / "demo").mkdir(parents=True)
        (repo / "demo" / "SKILL.md").write_text(
            '---\nname: demo\nversion: "1.0"\n---\nbase\n'
        )
        (repo / "demo" / "scripts").mkdir()
        (repo / "demo" / "scripts" / "tools.py").write_text("print('tools')\n")
        (repo / "demo" / "references").mkdir()
        (repo / "demo" / "references" / "doc.md").write_text("# ref\n")
        (global_dir / "demo").symlink_to(repo / "demo")

        vm = VersionManager(
            workspace, eval_base_dir=tmp_path / "eval",
            read_skill_dirs=[workspace, global_dir],
        )
        return vm, workspace, repo

    def test_publish_keeps_upstream_assets_reachable(self, tmp_path: Path):
        vm, workspace, repo = self._setup_layered_skill_with_assets(tmp_path)

        evolved = '---\nname: demo\nversion: "1.0-evolve"\n---\nimproved\n'
        vm.publish("demo", "1.0-evolve", evolved)

        # The exact path the SKILL.md tells the LLM to invoke:
        tools_py = workspace / "demo" / "scripts" / "tools.py"
        assert tools_py.exists(), \
            "publish must propagate upstream auxiliary subdirs into workspace"
        assert tools_py.read_text() == "print('tools')\n"

        ref_md = workspace / "demo" / "references" / "doc.md"
        assert ref_md.exists()
        assert ref_md.read_text() == "# ref\n"

        # Upstream stays untouched
        assert (repo / "demo" / "scripts" / "tools.py").read_text() == "print('tools')\n"

    def test_publish_does_not_clobber_existing_workspace_assets(self, tmp_path: Path):
        vm, workspace, _ = self._setup_layered_skill_with_assets(tmp_path)

        # User pre-populated their own scripts/ in workspace before publish
        (workspace / "demo").mkdir()
        (workspace / "demo" / "scripts").mkdir()
        (workspace / "demo" / "scripts" / "tools.py").write_text("# overridden\n")

        evolved = '---\nname: demo\nversion: "1.0-evolve"\n---\nimproved\n'
        vm.publish("demo", "1.0-evolve", evolved)

        # Existing override preserved, NOT replaced by upstream symlink
        assert (workspace / "demo" / "scripts" / "tools.py").read_text() == "# overridden\n"

    def test_publish_skips_dotdirs_and_skill_md(self, tmp_path: Path):
        vm, workspace, repo = self._setup_layered_skill_with_assets(tmp_path)
        # Add a hidden runtime-state dir upstream — agents must NOT inherit
        (repo / "demo" / ".invest").mkdir()
        (repo / "demo" / ".invest" / "graph.json").write_text("{}")

        evolved = '---\nname: demo\nversion: "1.0-evolve"\n---\nimproved\n'
        vm.publish("demo", "1.0-evolve", evolved)

        # Static subdirs propagated
        assert (workspace / "demo" / "scripts" / "tools.py").exists()
        # Hidden state dir NOT propagated
        assert not (workspace / "demo" / ".invest").exists()
        # SKILL.md is the freshly written content (not a symlink to upstream)
        skill_md = workspace / "demo" / "SKILL.md"
        assert not skill_md.is_symlink()
        assert "1.0-evolve" in skill_md.read_text()

    def test_publish_without_upstream_assets_still_works(self, tmp_path: Path):
        """Plain (no read-chain auxiliary content) publish path keeps working."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        vm = VersionManager(skills_dir)
        vm.publish("plain", "1.0", '---\nname: plain\nversion: "1.0"\n---\nbody\n')
        assert (skills_dir / "plain" / "SKILL.md").exists()
