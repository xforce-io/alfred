"""Test that $CM subcommand --repo <name> works (global args after subcommand).

tools.py defines --repo and --agent on the parent parser.  Standard argparse
rejects global args placed AFTER the subcommand.  These tests verify that
both orderings are accepted, matching the SKILL.md documentation.
"""

from __future__ import annotations

import sys
from pathlib import Path


_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# We only need the parser, not the full main().  Reconstruct it the same way.
import argparse  # noqa: E402


def _build_parser():
    """Import and build the argparse parser from tools.main() logic."""
    # Re-import to get the latest code
    import importlib
    import tools as _tools
    importlib.reload(_tools)

    # We can't easily extract the parser from main(), so we call parse_args
    # via subprocess or replicate the parser.  Instead, let's test via
    # sys.argv injection + SystemExit capture.
    return None  # not used; we test via _parse_cli below


def _parse_cli(argv: list[str]):
    """Run tools.py argument parsing on the given argv and return the Namespace.

    Raises SystemExit(2) if argparse rejects the arguments.
    """
    import tools as _tools
    import importlib
    importlib.reload(_tools)

    # Reconstruct the parser identically to main(), using _add_global_args
    from tools import MODES, _add_global_args

    parser = argparse.ArgumentParser(prog="cm", description="Coding Master v3")
    _add_global_args(parser, is_parent=True)
    sub = parser.add_subparsers(dest="command")

    p_start = sub.add_parser("start")
    _add_global_args(p_start)
    p_start.add_argument("--branch", default=None)
    p_start.add_argument("--plan-file", default=None)
    p_start.add_argument("--mode", default="deliver", choices=list(MODES.keys()))

    p_lock = sub.add_parser("lock")
    _add_global_args(p_lock)
    p_lock.add_argument("--branch", default=None)
    p_lock.add_argument("--mode", default="deliver", choices=list(MODES.keys()))

    for name in ("unlock", "status", "renew", "plan-ready", "integrate", "progress"):
        _add_global_args(sub.add_parser(name))

    p_claim = sub.add_parser("claim")
    _add_global_args(p_claim)
    p_claim.add_argument("--feature", "-f", required=True, type=int)

    p_scope = sub.add_parser("scope")
    _add_global_args(p_scope)
    p_scope.add_argument("--diff", default=None)
    p_scope.add_argument("--files", nargs="*", default=None)
    p_scope.add_argument("--pr", default=None)
    p_scope.add_argument("--goal", default=None)

    p_report = sub.add_parser("report")
    _add_global_args(p_report)
    p_report.add_argument("--content", default=None)
    p_report.add_argument("--file", default=None)

    p_submit = sub.add_parser("submit")
    _add_global_args(p_submit)
    p_submit.add_argument("--title", "-t", required=True)

    p_journal = sub.add_parser("journal")
    _add_global_args(p_journal)
    p_journal.add_argument("--message", "-m", required=True)

    p_doctor = sub.add_parser("doctor")
    _add_global_args(p_doctor)
    p_doctor.add_argument("--fix", action="store_true")

    return parser.parse_args(argv)


class TestGlobalArgsBeforeSubcommand:
    """Baseline: --repo before subcommand (already works)."""

    def test_lock_repo_before(self):
        args = _parse_cli(["--repo", "alfred", "lock", "--mode", "review"])
        assert args.repo == "alfred"
        assert args.command == "lock"
        assert args.mode == "review"

    def test_status_repo_before(self):
        args = _parse_cli(["--repo", "alfred", "status"])
        assert args.repo == "alfred"
        assert args.command == "status"

    def test_start_repo_before(self):
        args = _parse_cli(["--repo", "myrepo", "start", "--mode", "analyze"])
        assert args.repo == "myrepo"
        assert args.command == "start"
        assert args.mode == "analyze"


class TestGlobalArgsAfterSubcommand:
    """SKILL.md documents `$CM lock --repo <name>`.  This MUST work."""

    def test_lock_repo_after(self):
        args = _parse_cli(["lock", "--repo", "alfred", "--mode", "review"])
        assert args.repo == "alfred"
        assert args.command == "lock"
        assert args.mode == "review"

    def test_unlock_repo_after(self):
        args = _parse_cli(["unlock", "--repo", "alfred"])
        assert args.repo == "alfred"
        assert args.command == "unlock"

    def test_status_repo_after(self):
        args = _parse_cli(["status", "--repo", "alfred"])
        assert args.repo == "alfred"
        assert args.command == "status"

    def test_start_repo_after(self):
        args = _parse_cli(["start", "--repo", "myrepo", "--mode", "review"])
        assert args.repo == "myrepo"
        assert args.command == "start"
        assert args.mode == "review"

    def test_doctor_repo_after(self):
        args = _parse_cli(["doctor", "--repo", "alfred", "--fix"])
        assert args.repo == "alfred"
        assert args.command == "doctor"
        assert args.fix is True

    def test_lock_short_flag_after(self):
        """$CM lock -r alfred should also work."""
        args = _parse_cli(["lock", "-r", "alfred"])
        assert args.repo == "alfred"
        assert args.command == "lock"

    def test_agent_after_subcommand(self):
        """--agent is also a global arg, should work after subcommand."""
        args = _parse_cli(["status", "--repo", "alfred", "--agent", "demo"])
        assert args.repo == "alfred"
        assert args.agent == "demo"


class TestMixedOrdering:
    """Edge cases: args split across parent and subcommand positions."""

    def test_repo_before_mode_after(self):
        args = _parse_cli(["--repo", "alfred", "lock", "--mode", "debug"])
        assert args.repo == "alfred"
        assert args.mode == "debug"

    def test_agent_before_repo_after(self):
        args = _parse_cli(["--agent", "bot", "lock", "--repo", "alfred"])
        assert args.repo == "alfred"
        assert args.agent == "bot"
        assert args.command == "lock"
