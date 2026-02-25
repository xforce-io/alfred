#!/usr/bin/env python3
"""CLI router for coding-master skill."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure scripts/ is on sys.path for sibling imports
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from config_manager import ConfigManager
from workspace import WorkspaceManager, LockFile, ARTIFACT_DIR
from env_probe import EnvProber
from feature_manager import FeatureManager
from test_runner import TestRunner
from git_ops import GitOps
from engine.claude_runner import ClaudeRunner
from engine.codex_runner import CodexRunner


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Prompt templates
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ANALYZE_PROMPT = """\
## Development Environment (Workspace)
{workspace_snapshot}

## Runtime Environment (Env)
{env_snapshot}

## Task
Analyze the following issue. Do NOT modify any code.
Issue: {task}

Output:
1. Problem location: which files and functions are involved
2. Root cause analysis: correlate with runtime logs if available
3. Fix proposals (multiple if applicable, mark recommended)
4. Impact scope
5. Risk assessment (low / medium / high)
6. Whether more Env information is needed
"""

DEVELOP_PROMPT = """\
## Development Environment (Workspace)
{workspace_snapshot}

## Diagnosis Report
{analysis}

## User-Confirmed Plan
{plan}

## Task
Implement the fix based on the diagnosis report above.
Task: {task}

Rules:
- Only modify files within this repository
- Do NOT run tests — that will be done separately
- Do NOT commit — that will be done separately
- Keep changes minimal and focused
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Lock-aware wrapper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _resolve_workspace_path(args) -> str | None:
    """Get workspace path from args.workspace name."""
    config = ConfigManager()
    ws = config.get_workspace(args.workspace)
    if ws is None:
        return None
    return ws["path"]


def with_lock_update(workspace_path: str, phase: str, fn, *args, **kwargs) -> dict:
    """Verify lock → run fn → update phase → renew lease → save."""
    lock = LockFile(workspace_path)
    try:
        lock.verify_active()
    except RuntimeError as e:
        error_code = "LEASE_EXPIRED" if "expired" in str(e) else "LOCK_NOT_FOUND"
        return {"ok": False, "error": str(e), "error_code": error_code}

    result = fn(*args, **kwargs)

    # Update lock on success
    if isinstance(result, dict) and result.get("ok", True):
        lock.update_phase(phase)
        lock.renew_lease()
        lock.save()

    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Engine helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _get_engine(name: str):
    if name == "claude":
        return ClaudeRunner()
    if name == "codex":
        return CodexRunner()
    return None


def _load_artifact(ws_path: str, filename: str) -> str:
    p = Path(ws_path) / ARTIFACT_DIR / filename
    if p.exists():
        return p.read_text()
    return "(not available)"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Command handlers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def cmd_config_list(args) -> dict:
    return ConfigManager().list_all()


def cmd_config_add(args) -> dict:
    return ConfigManager().add(args.kind, args.name, args.value)


def cmd_config_set(args) -> dict:
    return ConfigManager().set_field(args.kind, args.name, args.key, args.value)


def cmd_config_remove(args) -> dict:
    return ConfigManager().remove(args.kind, args.name)


def cmd_workspace_check(args) -> dict:
    config = ConfigManager()
    engine = args.engine or config.get_default_engine()
    mgr = WorkspaceManager(config)
    return mgr.check_and_acquire(args.workspace, args.task, engine)


def cmd_env_probe(args) -> dict:
    ws_path = _resolve_workspace_path(args)
    if ws_path is None:
        return {"ok": False, "error": f"workspace '{args.workspace}' not found",
                "error_code": "PATH_NOT_FOUND"}

    config = ConfigManager()
    prober = EnvProber(config)
    extra = args.commands if hasattr(args, "commands") and args.commands else None

    def do_probe():
        result = prober.probe(args.env, extra_commands=extra)
        if result.get("ok") and result.get("data"):
            # Save artifact
            art_dir = Path(ws_path) / ARTIFACT_DIR
            art_dir.mkdir(exist_ok=True)
            snap_path = art_dir / "env_snapshot.json"
            snap_path.write_text(
                json.dumps(result["data"], indent=2, ensure_ascii=False)
            )
            lock = LockFile(ws_path)
            if lock.exists():
                lock.load()
                lock.add_artifact("env_snapshot", f"{ARTIFACT_DIR}/env_snapshot.json")
                lock.save()
        return result

    return with_lock_update(ws_path, "env-probe", do_probe)


def cmd_analyze(args) -> dict:
    ws_path = _resolve_workspace_path(args)
    if ws_path is None:
        return {"ok": False, "error": f"workspace '{args.workspace}' not found",
                "error_code": "PATH_NOT_FOUND"}

    config = ConfigManager()
    engine_name = args.engine or config.get_default_engine()
    engine = _get_engine(engine_name)
    if engine is None:
        return {"ok": False, "error": f"unknown engine: {engine_name}",
                "error_code": "ENGINE_ERROR"}

    max_turns = config.get_max_turns()
    ws_snapshot = _load_artifact(ws_path, "workspace_snapshot.json")
    env_snapshot = _load_artifact(ws_path, "env_snapshot.json")

    prompt = ANALYZE_PROMPT.format(
        workspace_snapshot=ws_snapshot,
        env_snapshot=env_snapshot,
        task=args.task,
    )

    def do_analyze():
        result = engine.run(ws_path, prompt, max_turns=max_turns)
        if result.success:
            # Save analysis artifact only on success
            art_dir = Path(ws_path) / ARTIFACT_DIR
            art_dir.mkdir(exist_ok=True)
            analysis_path = art_dir / "phase2_analysis.md"
            analysis_path.write_text(result.summary)
            lock = LockFile(ws_path)
            if lock.exists():
                lock.load()
                lock.add_artifact("analysis_report", f"{ARTIFACT_DIR}/phase2_analysis.md")
                lock.save()
        return {
            "ok": result.success,
            "data": {
                "summary": result.summary,
                "files_changed": result.files_changed,
            },
            **({"error": result.error, "error_code": "ENGINE_ERROR"} if result.error else {}),
        }

    return with_lock_update(ws_path, "analyzing", do_analyze)


def cmd_develop(args) -> dict:
    ws_path = _resolve_workspace_path(args)
    if ws_path is None:
        return {"ok": False, "error": f"workspace '{args.workspace}' not found",
                "error_code": "PATH_NOT_FOUND"}

    config = ConfigManager()
    engine_name = args.engine or config.get_default_engine()
    engine = _get_engine(engine_name)
    if engine is None:
        return {"ok": False, "error": f"unknown engine: {engine_name}",
                "error_code": "ENGINE_ERROR"}

    max_turns = config.get_max_turns()
    ws_snapshot = _load_artifact(ws_path, "workspace_snapshot.json")
    analysis = _load_artifact(ws_path, "phase2_analysis.md")

    def do_develop():
        # Create branch
        if args.branch:
            git = GitOps(ws_path)
            br_result = git.create_branch(args.branch)
            # Update lock with branch name
            lock = LockFile(ws_path)
            if lock.exists():
                lock.load()
                lock.data["branch"] = args.branch
                lock.save()

        prompt = DEVELOP_PROMPT.format(
            workspace_snapshot=ws_snapshot,
            analysis=analysis,
            plan=args.plan or "(proceed with recommended approach)",
            task=args.task,
        )

        result = engine.run(ws_path, prompt, max_turns=max_turns)
        return {
            "ok": result.success,
            "data": {
                "summary": result.summary,
                "files_changed": result.files_changed,
            },
            **({"error": result.error} if result.error else {}),
        }

    return with_lock_update(ws_path, "developing", do_develop)


def cmd_test(args) -> dict:
    ws_path = _resolve_workspace_path(args)
    if ws_path is None:
        return {"ok": False, "error": f"workspace '{args.workspace}' not found",
                "error_code": "PATH_NOT_FOUND"}

    config = ConfigManager()
    runner = TestRunner(config)

    def do_test():
        result = runner.run(args.workspace)
        if result.get("ok"):
            lock = LockFile(ws_path)
            if lock.exists():
                lock.load()
                lock.add_artifact("test_report", f"{ARTIFACT_DIR}/test_report.json")
                lock.save()
        return result

    return with_lock_update(ws_path, "testing", do_test)


def cmd_submit_pr(args) -> dict:
    ws_path = _resolve_workspace_path(args)
    if ws_path is None:
        return {"ok": False, "error": f"workspace '{args.workspace}' not found",
                "error_code": "PATH_NOT_FOUND"}

    git = GitOps(ws_path)

    def do_submit():
        result = git.submit_pr(
            title=args.title,
            body=args.body or "",
            commit_message=args.title,
        )
        # Track pushed_to_remote in lock
        if result.get("ok"):
            lock = LockFile(ws_path)
            if lock.exists():
                lock.load()
                lock.data["pushed_to_remote"] = True
                lock.save()
        return result

    return with_lock_update(ws_path, "submitted", do_submit)


def cmd_env_verify(args) -> dict:
    ws_path = _resolve_workspace_path(args)
    if ws_path is None:
        return {"ok": False, "error": f"workspace '{args.workspace}' not found",
                "error_code": "PATH_NOT_FOUND"}

    config = ConfigManager()
    prober = EnvProber(config)
    baseline_path = str(Path(ws_path) / ARTIFACT_DIR / "env_snapshot.json")

    def do_verify():
        result = prober.verify(args.env, baseline_path)
        if result.get("ok") and result.get("data"):
            # Save verification report
            art_dir = Path(ws_path) / ARTIFACT_DIR
            art_dir.mkdir(exist_ok=True)
            report_path = art_dir / "env_verify_report.json"
            report_path.write_text(
                json.dumps(result["data"], indent=2, ensure_ascii=False)
            )
            lock = LockFile(ws_path)
            if lock.exists():
                lock.load()
                lock.add_artifact(
                    "env_verify_report",
                    f"{ARTIFACT_DIR}/env_verify_report.json",
                )
                lock.save()
        return result

    return with_lock_update(ws_path, "env-verified", do_verify)


def cmd_release(args) -> dict:
    config = ConfigManager()
    mgr = WorkspaceManager(config)
    cleanup = getattr(args, "cleanup", False)
    return mgr.release(args.workspace, cleanup=cleanup)


def cmd_renew_lease(args) -> dict:
    config = ConfigManager()
    mgr = WorkspaceManager(config)
    return mgr.renew_lease(args.workspace)


# ── Feature management ───────────────────────────────────

def cmd_feature_plan(args) -> dict:
    ws_path = _resolve_workspace_path(args)
    if ws_path is None:
        return {"ok": False, "error": f"workspace '{args.workspace}' not found",
                "error_code": "PATH_NOT_FOUND"}
    fm = FeatureManager(ws_path)
    features = json.loads(args.features)
    return fm.create_plan(args.task, features)


def cmd_feature_next(args) -> dict:
    ws_path = _resolve_workspace_path(args)
    if ws_path is None:
        return {"ok": False, "error": f"workspace '{args.workspace}' not found",
                "error_code": "PATH_NOT_FOUND"}
    fm = FeatureManager(ws_path)
    return fm.next_feature()


def cmd_feature_done(args) -> dict:
    ws_path = _resolve_workspace_path(args)
    if ws_path is None:
        return {"ok": False, "error": f"workspace '{args.workspace}' not found",
                "error_code": "PATH_NOT_FOUND"}
    fm = FeatureManager(ws_path)
    return fm.mark_done(
        index=args.index,
        branch=getattr(args, "branch", None),
        pr=getattr(args, "pr", None),
    )


def cmd_feature_list(args) -> dict:
    ws_path = _resolve_workspace_path(args)
    if ws_path is None:
        return {"ok": False, "error": f"workspace '{args.workspace}' not found",
                "error_code": "PATH_NOT_FOUND"}
    fm = FeatureManager(ws_path)
    return fm.list_all()


def cmd_feature_update(args) -> dict:
    ws_path = _resolve_workspace_path(args)
    if ws_path is None:
        return {"ok": False, "error": f"workspace '{args.workspace}' not found",
                "error_code": "PATH_NOT_FOUND"}
    fm = FeatureManager(ws_path)
    return fm.update(
        index=args.index,
        status=getattr(args, "status", None),
        title=getattr(args, "title", None),
        task=getattr(args, "task_desc", None),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Argument parser
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dispatch.py", description="Coding Master CLI")
    sub = p.add_subparsers(dest="command", required=True)

    # ── Config ──────────────────────────────────────────────
    sub.add_parser("config-list", help="List all config")

    ca = sub.add_parser("config-add", help="Add workspace or env")
    ca.add_argument("kind", choices=["workspace", "env"])
    ca.add_argument("name")
    ca.add_argument("value")

    cs = sub.add_parser("config-set", help="Set a field on workspace or env")
    cs.add_argument("kind", choices=["workspace", "env"])
    cs.add_argument("name")
    cs.add_argument("key")
    cs.add_argument("value")

    cr = sub.add_parser("config-remove", help="Remove workspace or env")
    cr.add_argument("kind", choices=["workspace", "env"])
    cr.add_argument("name")

    # ── Workflow ────────────────────────────────────────────
    wc = sub.add_parser("workspace-check", help="Check and acquire workspace")
    wc.add_argument("--workspace", required=True)
    wc.add_argument("--task", required=True)
    wc.add_argument("--engine", default=None)

    ep = sub.add_parser("env-probe", help="Probe runtime environment")
    ep.add_argument("--workspace", required=True)
    ep.add_argument("--env", required=True)
    ep.add_argument("--commands", nargs="*", default=None)

    az = sub.add_parser("analyze", help="Analyze issue with coding engine")
    az.add_argument("--workspace", required=True)
    az.add_argument("--task", required=True)
    az.add_argument("--engine", default=None)

    dv = sub.add_parser("develop", help="Develop fix with coding engine")
    dv.add_argument("--workspace", required=True)
    dv.add_argument("--task", required=True)
    dv.add_argument("--plan", default=None)
    dv.add_argument("--branch", default=None)
    dv.add_argument("--engine", default=None)

    ts = sub.add_parser("test", help="Run lint + tests")
    ts.add_argument("--workspace", required=True)

    sp = sub.add_parser("submit-pr", help="Commit, push, create PR")
    sp.add_argument("--workspace", required=True)
    sp.add_argument("--title", required=True)
    sp.add_argument("--body", default="")

    ev = sub.add_parser("env-verify", help="Verify fix in deployment env")
    ev.add_argument("--workspace", required=True)
    ev.add_argument("--env", required=True)

    rl = sub.add_parser("release", help="Release workspace lock")
    rl.add_argument("--workspace", required=True)
    rl.add_argument("--cleanup", action="store_true")

    rn = sub.add_parser("renew-lease", help="Renew workspace lock lease")
    rn.add_argument("--workspace", required=True)

    # ── Feature management ──────────────────────────────────
    fp = sub.add_parser("feature-plan", help="Create feature split plan")
    fp.add_argument("--workspace", required=True)
    fp.add_argument("--task", required=True)
    fp.add_argument("--features", required=True, help="JSON array of {title, task, depends_on?}")

    fn = sub.add_parser("feature-next", help="Get next executable feature")
    fn.add_argument("--workspace", required=True)

    fd = sub.add_parser("feature-done", help="Mark feature as done")
    fd.add_argument("--workspace", required=True)
    fd.add_argument("--index", type=int, required=True)
    fd.add_argument("--branch", default=None)
    fd.add_argument("--pr", default=None)

    fl = sub.add_parser("feature-list", help="List all features and status")
    fl.add_argument("--workspace", required=True)

    fu = sub.add_parser("feature-update", help="Update a feature")
    fu.add_argument("--workspace", required=True)
    fu.add_argument("--index", type=int, required=True)
    fu.add_argument("--status", default=None, choices=["pending", "in_progress", "done", "skipped"])
    fu.add_argument("--title", default=None)
    fu.add_argument("--task-desc", default=None)

    return p


COMMANDS = {
    "config-list": cmd_config_list,
    "config-add": cmd_config_add,
    "config-set": cmd_config_set,
    "config-remove": cmd_config_remove,
    "workspace-check": cmd_workspace_check,
    "env-probe": cmd_env_probe,
    "analyze": cmd_analyze,
    "develop": cmd_develop,
    "test": cmd_test,
    "submit-pr": cmd_submit_pr,
    "env-verify": cmd_env_verify,
    "release": cmd_release,
    "renew-lease": cmd_renew_lease,
    "feature-plan": cmd_feature_plan,
    "feature-next": cmd_feature_next,
    "feature-done": cmd_feature_done,
    "feature-list": cmd_feature_list,
    "feature-update": cmd_feature_update,
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Entry point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    handler = COMMANDS.get(args.command)
    if handler is None:
        result = {"ok": False, "error": f"unknown command: {args.command}"}
    else:
        try:
            result = handler(args)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
