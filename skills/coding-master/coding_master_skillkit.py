"""CodingMaster Skillkit — 将 cm 命令注册为 Dolphin 原生 tools.

取代 _bash + cm CLI 的间接调用模式，让 agent 直接调用结构化的
_cm_* tools，实现技术层面的约束而非叙述约束。

加载方式：config.yaml 中 per-agent skillkit_dirs 配置
DPH 引用：tools=[coding_master, _date]
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from typing import List

from dolphin.core.skill.skill_function import SkillFunction
from dolphin.core.skill.skillkit import Skillkit

logger = logging.getLogger(__name__)

# Path to the scripts directory containing tools.py and config_manager.py
_SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"

# Lazy-loaded module reference
_tools_module = None


def _get_tools():
    """Lazy-import tools module from scripts/."""
    global _tools_module
    if _tools_module is not None:
        return _tools_module

    scripts_dir = str(_SCRIPTS_DIR)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    import tools as cm_tools  # noqa: E402

    _tools_module = cm_tools
    return _tools_module


def _make_args(**kwargs) -> Namespace:
    """Build an argparse-compatible Namespace with sensible defaults."""
    defaults = {
        "repo": None,
        "agent": None,
        "branch": None,
        "mode": "deliver",
        "force": False,
        "feature": None,
        "stash": False,
        "plan_file": None,
        "file": None,
        "lines": None,
        "pattern": None,
        "path": None,
        "ignore_case": False,
        "diff": None,
        "files": None,
        "pr": None,
        "goal": None,
        "content": None,
        "title": None,
        "message": None,
        "fix": False,
        "engine": "claude-code",
        "timeout": 600,
        "max_turns": 30,
        # v4.5 file operations
        "start_line": None,
        "end_line": None,
        "max_results": None,
        "context": None,
        "glob": None,
        "old_text": None,
        "new_text": None,
        "base_ref": None,
    }
    defaults.update(kwargs)
    return Namespace(**defaults)


def _result_to_str(result: dict) -> str:
    """Serialize cmd_* return dict to JSON string for agent consumption."""
    return json.dumps(result, indent=2, ensure_ascii=False)


def _safe_call(fn, *args, **kwargs) -> dict:
    """Call a cmd_* function and return its result dict. Catches SystemExit so
    the daemon process is not killed when tools._fail() is invoked internally.
    tools._fail() prints a JSON error to stdout before calling sys.exit(1), so
    we redirect stdout to capture that JSON and return it as a dict."""
    import io
    import contextlib

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            return fn(*args, **kwargs)
    except SystemExit:
        captured = buf.getvalue().strip()
        if captured:
            try:
                return json.loads(captured)
            except json.JSONDecodeError:
                pass
        return {"ok": False, "error": "command failed"}


def _safe_cmd(fn, *args, **kwargs) -> str:
    """Call a cmd_* function and return JSON string. See _safe_call."""
    return _result_to_str(_safe_call(fn, *args, **kwargs))


# Git subcommands that _cm_git allows
_GIT_ALLOWED = frozenset({
    "add", "branch", "checkout", "commit", "diff", "log",
    "merge", "push", "rebase", "reset", "stash", "status",
    "show", "tag", "pull", "fetch", "cherry-pick",
})

# Git subcommands that mutate the repo — blocked in read-only modes
_GIT_MUTATING = frozenset({
    "add", "commit", "push", "merge", "rebase", "cherry-pick", "reset", "stash",
})



class CodingMasterSkillkit(Skillkit):
    """Dolphin Skillkit exposing cm commands as native agent tools.

    Eliminates the need for _bash by providing structured, schema-driven
    tools that enforce workspace conventions at the technical level.
    """

    def __init__(self, agent_id: str = "") -> None:
        super().__init__()
        self._agent_id = agent_id
        self._overlay_mode: str | None = None  # set when read-only overlay on deliver session

    def getName(self) -> str:
        return "coding_master"

    # ──────────────────────────────────────────────────────────
    #  Session lifecycle
    # ──────────────────────────────────────────────────────────

    def _cm_repos(self, **kwargs) -> str:
        """列出所有已配置的代码仓库和工作区。

        Returns:
            str: JSON — 包含 repos, workspaces, envs 列表
        """
        tools = _get_tools()
        return _safe_cmd(tools.cmd_repos, _make_args())

    def _cm_start(self, repo: str, mode: str = "deliver",
                  branch: str = "", plan_file: str = "", **kwargs) -> str:
        """一键启动会话：lock + 复制 plan + plan-ready。失败时自动回滚。

        Args:
            repo (str): 目标仓库名称
            mode (str): 会话模式 — deliver, review, debug, analyze
            branch (str): 可选的开发分支名
            plan_file (str): 可选的 PLAN.md 文件路径

        Returns:
            str: JSON — 包含 branch, session_worktree, plan 信息
        """
        tools = _get_tools()
        args = _make_args(
            repo=repo, mode=mode,
            branch=branch or None,
            plan_file=plan_file or None,
            agent=self._agent_id,
        )
        return _safe_cmd(tools.cmd_start, args)

    def _cm_lock(self, repo: str, mode: str = "deliver",
                 branch: str = "", **kwargs) -> str:
        """锁定工作区，创建开发分支（review/analyze 为只读锁）。

        如果已有活跃会话，自动加入而非创建新会话。

        Args:
            repo (str): 目标仓库名称
            mode (str): 会话模式 — deliver, review, debug, analyze
            branch (str): 可选的开发分支名

        Returns:
            str: JSON — 包含 branch, session_worktree, mode 信息
        """
        tools = _get_tools()
        args = _make_args(
            repo=repo, mode=mode,
            branch=branch or None,
            agent=self._agent_id,
        )
        result = _safe_call(tools.cmd_lock, args)
        # Track overlay mode: when a read-only lock overlays a deliver session,
        # lock.json still says "deliver" but this agent operates in review mode.
        if result.get("ok") and result.get("data", {}).get("overlay"):
            self._overlay_mode = mode
        else:
            self._overlay_mode = None
        return _result_to_str(result)

    def _cm_unlock(self, repo: str = "", force: bool = False, **kwargs) -> str:
        """释放工作区锁。写会话未完成时需要 force=true。

        Args:
            repo (str): 目标仓库名称（可省略，自动检测）
            force (bool): 强制解锁，即使写会话尚未完成

        Returns:
            str: JSON — 解锁结果
        """
        tools = _get_tools()
        args = _make_args(repo=repo or None, force=force, agent=self._agent_id)
        return _safe_cmd(tools.cmd_unlock, args)

    def _cm_status(self, repo: str = "", **kwargs) -> str:
        """查看当前工作区锁状态、会话模式、features 进度。

        Args:
            repo (str): 目标仓库名称（可省略，自动检测）

        Returns:
            str: JSON — lock 状态、mode、features 摘要
        """
        tools = _get_tools()
        args = _make_args(repo=repo or None, agent=self._agent_id)
        return _safe_cmd(tools.cmd_status, args)

    # ──────────────────────────────────────────────────────────
    #  Feature delivery pipeline
    # ──────────────────────────────────────────────────────────

    def _cm_claim(self, repo: str = "", feature: int = 0, **kwargs) -> str:
        """认领一个 feature，创建独立 worktree 和分支。

        Args:
            repo (str): 目标仓库名称
            feature (int): Feature 编号（对应 PLAN.md 中的 Feature N）

        Returns:
            str: JSON — 包含 branch, worktree 路径
        """
        tools = _get_tools()
        args = _make_args(repo=repo or None, feature=feature, agent=self._agent_id)
        return _safe_cmd(tools.cmd_claim, args)

    def _cm_dev(self, repo: str = "", feature: int = 0, **kwargs) -> str:
        """将 feature 推进到 developing 阶段。需要先完成 Analysis 和 Plan。

        Args:
            repo (str): 目标仓库名称
            feature (int): Feature 编号

        Returns:
            str: JSON — 包含 worktree 路径和当前状态
        """
        tools = _get_tools()
        args = _make_args(repo=repo or None, feature=feature, agent=self._agent_id)
        return _safe_cmd(tools.cmd_dev, args)

    def _cm_test(self, repo: str = "", feature: int = 0, **kwargs) -> str:
        """运行 feature 的测试 + lint + typecheck，写入 evidence。

        Args:
            repo (str): 目标仓库名称
            feature (int): Feature 编号

        Returns:
            str: JSON — 测试结果和 evidence 路径
        """
        tools = _get_tools()
        args = _make_args(repo=repo or None, feature=feature, agent=self._agent_id)
        return _safe_cmd(tools.cmd_test, args)

    def _cm_done(self, repo: str = "", feature: int = 0, **kwargs) -> str:
        """标记 feature 完成。需要通过测试且有 evidence。

        Args:
            repo (str): 目标仓库名称
            feature (int): Feature 编号

        Returns:
            str: JSON — 完成状态
        """
        tools = _get_tools()
        args = _make_args(repo=repo or None, feature=feature, agent=self._agent_id)
        return _safe_cmd(tools.cmd_done, args)

    def _cm_reopen(self, repo: str = "", feature: int = 0, **kwargs) -> str:
        """重新打开已完成的 feature 进行修复。

        Args:
            repo (str): 目标仓库名称
            feature (int): Feature 编号

        Returns:
            str: JSON — 重开后的状态
        """
        tools = _get_tools()
        args = _make_args(repo=repo or None, feature=feature, agent=self._agent_id)
        return _safe_cmd(tools.cmd_reopen, args)

    def _cm_integrate(self, repo: str = "", **kwargs) -> str:
        """合并所有 done features 到开发分支，运行集成测试。

        Args:
            repo (str): 目标仓库名称

        Returns:
            str: JSON — 合并结果和集成测试报告
        """
        tools = _get_tools()
        args = _make_args(repo=repo or None, agent=self._agent_id)
        return _safe_cmd(tools.cmd_integrate, args)

    def _cm_submit(self, repo: str = "", title: str = "", **kwargs) -> str:
        """Push 代码并创建 PR，清理 worktrees。

        Args:
            repo (str): 目标仓库名称
            title (str): PR 标题

        Returns:
            str: JSON — PR URL 和清理结果
        """
        tools = _get_tools()
        args = _make_args(repo=repo or None, title=title, agent=self._agent_id)
        return _safe_cmd(tools.cmd_submit, args)

    # ──────────────────────────────────────────────────────────
    #  Analysis / Review mode
    # ──────────────────────────────────────────────────────────

    def _cm_scope(self, repo: str = "", diff: str = "", files: str = "",
                  pr: str = "", goal: str = "", **kwargs) -> str:
        """定义分析/review 的范围。用于 review, debug, analyze 模式。

        Args:
            repo (str): 目标仓库名称
            diff (str): Diff range，如 HEAD~3..HEAD
            files (str): 文件路径或 glob，空格分隔
            pr (str): PR 编号或 URL
            goal (str): 分析目标描述

        Returns:
            str: JSON — scope 定义结果
        """
        tools = _get_tools()
        files_list = files.split() if files else None
        args = _make_args(
            repo=repo or None,
            diff=diff or None,
            files=files_list,
            pr=pr or None,
            goal=goal or None,
            agent=self._agent_id,
            mode_override=self._overlay_mode,
        )
        return _safe_cmd(tools.cmd_scope, args)

    def _cm_report(self, repo: str = "", content: str = "",
                   file: str = "", **kwargs) -> str:
        """写入会话报告或诊断。用于 review, debug, analyze 模式。

        Args:
            repo (str): 目标仓库名称
            content (str): 报告内容（直接传入）
            file (str): 报告文件路径（二选一）

        Returns:
            str: JSON — 报告写入结果
        """
        tools = _get_tools()
        args = _make_args(
            repo=repo or None,
            content=content or None,
            file=file or None,
            agent=self._agent_id,
            mode_override=self._overlay_mode,
        )
        return _safe_cmd(tools.cmd_report, args)

    def _cm_engine_run(self, repo: str = "", goal: str = "",
                       engine: str = "claude-code", timeout: int = 600,
                       max_turns: int = 30, **kwargs) -> str:
        """委托引擎子进程执行代码分析。需先定义 scope。

        引擎在子进程中读取 scope 内所有文件，分析代码并返回结构化结果。
        review/analyze 模式优先使用此命令，避免手动逐文件 read。

        Args:
            repo (str): 目标仓库名称
            goal (str): 分析目标（覆盖 scope 中的 goal）
            engine (str): 引擎名称，默认 claude-code
            timeout (int): 超时秒数，默认 600
            max_turns (int): 引擎最大轮次，默认 30

        Returns:
            str: JSON — 包含 summary, findings, files_analyzed 等
        """
        tools = _get_tools()
        args = _make_args(
            repo=repo or None,
            goal=goal or None,
            engine=engine,
            timeout=timeout,
            max_turns=max_turns,
            agent=self._agent_id,
        )
        return _safe_cmd(tools.cmd_engine_run, args)

    # ──────────────────────────────────────────────────────────
    #  Utility
    # ──────────────────────────────────────────────────────────

    def _cm_progress(self, repo: str = "", **kwargs) -> str:
        """显示当前会话进度、feature 状态和下一步建议。

        Args:
            repo (str): 目标仓库名称

        Returns:
            str: JSON — 进度摘要和行动建议
        """
        tools = _get_tools()
        args = _make_args(repo=repo or None, agent=self._agent_id)
        return _safe_cmd(tools.cmd_progress, args)

    def _cm_journal(self, message: str, repo: str = "", **kwargs) -> str:
        """向 JOURNAL.md 追加一条日志。

        Args:
            message (str): 日志消息
            repo (str): 目标仓库名称

        Returns:
            str: JSON — 写入结果
        """
        tools = _get_tools()
        args = _make_args(
            repo=repo or None, message=message,
            agent=self._agent_id,
        )
        return _safe_cmd(tools.cmd_journal, args)

    def _cm_regression(self, repo: str = "", **kwargs) -> str:
        """全量回归测试：lint + typecheck + tests，在 session worktree 上运行。

        不需要 feature 流程，不写 evidence/claims，只返回结果。
        命令可通过项目根目录的 .coding-master.toml 配置覆盖。

        Args:
            repo (str): 目标仓库名称

        Returns:
            str: JSON — 包含 overall, lint, typecheck, test 各项结果
        """
        tools = _get_tools()
        args = _make_args(repo=repo or None, agent=self._agent_id)
        return _safe_cmd(tools.cmd_regression, args)

    def _cm_change_summary(self, repo: str = "", base_ref: str = "", **kwargs) -> str:
        """生成变更摘要：包含 unified diff、worktree 路径、commit 信息。

        用于向用户报告代码变更时，提供可 review 的完整信息。
        完成代码修改后应调用此命令，让用户可以看到实际 diff 和本地路径。

        Args:
            repo (str): 目标仓库名称
            base_ref (str): Diff 基准 ref（默认使用会话分支）

        Returns:
            str: JSON — 包含 diff, worktree, commit, review_command
        """
        tools = _get_tools()
        args = _make_args(repo=repo or None, base_ref=base_ref or None, agent=self._agent_id)
        return _safe_cmd(tools.cmd_change_summary, args)

    def _cm_doctor(self, repo: str = "", fix: bool = False, **kwargs) -> str:
        """诊断工作区状态并可选自动修复。

        Args:
            repo (str): 目标仓库名称
            fix (bool): 是否自动修复发现的问题

        Returns:
            str: JSON — 诊断结果和修复记录
        """
        tools = _get_tools()
        args = _make_args(repo=repo or None, fix=fix, agent=self._agent_id)
        return _safe_cmd(tools.cmd_doctor, args)

    # ──────────────────────────────────────────────────────────
    #  File operations (v4.5)
    # ──────────────────────────────────────────────────────────

    def _cm_read(self, repo: str = "", file: str = "",
                 start_line: int = 0, end_line: int = 0,
                 feature: int = 0, **kwargs) -> str:
        """读取文件内容，支持行范围。自动感知 session/feature worktree。

        Args:
            repo (str): 目标仓库名称
            file (str): 文件路径（绝对路径或相对于 worktree）
            start_line (int): 起始行号（1-based，0 表示从头开始）
            end_line (int): 结束行号（inclusive，0 表示到文件末尾）
            feature (int): 可选的 feature 编号（在 feature worktree 中查找）

        Returns:
            str: JSON — 包含带行号的文件内容
        """
        tools = _get_tools()
        args = _make_args(
            repo=repo or None, file=file,
            start_line=start_line or None,
            end_line=end_line or None,
            feature=feature or None,
            agent=self._agent_id,
        )
        return _safe_cmd(tools.cmd_read, args)

    def _cm_find(self, repo: str = "", pattern: str = "",
                 max_results: int = 50, feature: int = 0, **kwargs) -> str:
        """按 glob 模式查找文件。自动感知 session/feature worktree。

        Args:
            repo (str): 目标仓库名称
            pattern (str): Glob 模式（如 '**/*.py', 'src/**/test_*.py'）
            max_results (int): 最大返回数（默认 50）
            feature (int): 可选的 feature 编号

        Returns:
            str: JSON — 匹配的文件路径列表
        """
        tools = _get_tools()
        args = _make_args(
            repo=repo or None, pattern=pattern,
            max_results=max_results,
            feature=feature or None,
            agent=self._agent_id,
        )
        return _safe_cmd(tools.cmd_find, args)

    def _cm_grep(self, repo: str = "", pattern: str = "",
                 glob: str = "", context: int = 2,
                 max_results: int = 20, feature: int = 0, **kwargs) -> str:
        """搜索文件内容，返回匹配行。自动感知 session/feature worktree。

        Args:
            repo (str): 目标仓库名称
            pattern (str): 正则表达式
            glob (str): 文件过滤 glob（如 '*.py'）
            context (int): 上下文行数（默认 2）
            max_results (int): 最大匹配数（默认 20）
            feature (int): 可选的 feature 编号

        Returns:
            str: JSON — 匹配行及上下文
        """
        tools = _get_tools()
        args = _make_args(
            repo=repo or None, pattern=pattern,
            glob=glob or None,
            context=context,
            max_results=max_results,
            feature=feature or None,
            agent=self._agent_id,
        )
        return _safe_cmd(tools.cmd_grep, args)

    def _cm_edit(self, repo: str = "", file: str = "",
                 old_text: str = "", new_text: str = "",
                 feature: int = 0, **kwargs) -> str:
        """精确替换编辑文件。仅在 deliver/debug 模式下可用。

        old_text 必须在文件中唯一匹配，确保替换安全。

        Args:
            repo (str): 目标仓库名称
            file (str): 文件路径
            old_text (str): 要替换的原文（必须唯一匹配）
            new_text (str): 替换后的文本
            feature (int): 可选的 feature 编号

        Returns:
            str: JSON — 编辑结果
        """
        tools = _get_tools()
        args = _make_args(
            repo=repo or None, file=file,
            old_text=old_text, new_text=new_text,
            feature=feature or None,
            agent=self._agent_id,
        )
        return _safe_cmd(tools.cmd_edit, args)

    # ──────────────────────────────────────────────────────────
    #  Escape hatches (controlled)
    # ──────────────────────────────────────────────────────────

    def _cm_git(self, subcmd: str, args: str = "", cwd: str = "", **kwargs) -> str:
        """在工作区内执行 git 操作。仅允许安全的 git 子命令。

        Allowed: add, branch, checkout, cherry-pick, commit, diff, fetch,
                 log, merge, pull, push, rebase, reset, show, stash, status, tag

        Args:
            subcmd (str): git 子命令，如 commit, diff, log
            args (str): 子命令参数，如 '-m "fix bug"', '--oneline -5'
            cwd (str): 工作目录（默认使用当前会话的 worktree）

        Returns:
            str: JSON — 包含 stdout, stderr, returncode
        """
        if subcmd not in _GIT_ALLOWED:
            return _result_to_str({
                "ok": False,
                "error": f"git {subcmd} is not allowed. "
                         f"Allowed: {', '.join(sorted(_GIT_ALLOWED))}",
            })

        work_dir = cwd or self._resolve_session_cwd()
        if not work_dir:
            return _result_to_str({
                "ok": False,
                "error": "No active session. Lock a repo first, or specify cwd.",
            })

        # Block mutating git commands in read-only modes or without a developing feature
        if subcmd in _GIT_MUTATING:
            lock = self._find_active_lock(work_dir if cwd else None)
            if lock:
                mode = lock.get("mode", "deliver")
                if mode in ("review", "analyze"):
                    return _result_to_str({
                        "ok": False,
                        "error": f"git {subcmd} not allowed in {mode} mode (read-only). "
                                 "Switch to deliver mode first.",
                    })
                if mode == "deliver" and subcmd in ("add", "commit"):
                    repo_path = Path(lock.get("_repo_path", "")) if lock.get("_repo_path") else None
                    if repo_path:
                        tools = _get_tools()
                        claims = tools._atomic_json_read(repo_path / tools.CM_DIR / "claims.json")
                        features = claims.get("features", {}) if claims else {}
                        has_developing = any(
                            f.get("phase") == "developing" for f in features.values()
                        )
                        if not has_developing:
                            phase = lock.get("session_phase", "locked")
                            if phase == "locked":
                                hint = ("Session is in 'locked' phase — create "
                                        ".coding-master/PLAN.md first, then run "
                                        "cm plan-ready to validate it.")
                            elif phase == "reviewed":
                                hint = ("Session is in 'reviewed' phase — run "
                                        "cm claim --feature N to claim a feature, "
                                        "then cm dev --feature N to start developing.")
                            else:
                                hint = "Run cm claim + cm dev first."
                            return _result_to_str({
                                "ok": False,
                                "error": f"git {subcmd} requires a feature in "
                                         f"'developing' phase. {hint}",
                            })

        try:
            cmd_parts = ["git", subcmd]
            if args:
                cmd_parts.extend(shlex.split(args))

            result = subprocess.run(
                cmd_parts,
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=60,
            )
            return _result_to_str({
                "ok": result.returncode == 0,
                "data": {
                    "stdout": result.stdout[-4000:] if len(result.stdout) > 4000 else result.stdout,
                    "stderr": result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
                    "returncode": result.returncode,
                },
            })
        except subprocess.TimeoutExpired:
            return _result_to_str({"ok": False, "error": "git command timed out (60s limit)"})
        except Exception as exc:
            logger.debug("_cm_git failed: %s", exc, exc_info=True)
            return _result_to_str({"ok": False, "error": str(exc)})


    # ──────────────────────────────────────────────────────────
    #  Internal helpers
    # ──────────────────────────────────────────────────────────

    def _find_active_lock(self, cwd: str | None = None) -> dict | None:
        """Find active lock data with repo path injected as _repo_path."""
        tools = _get_tools()
        target_path = Path(cwd).expanduser().resolve() if cwd else None
        try:
            cfg = tools.ConfigManager()
            section = cfg._section()
            workspaces = section.get("workspaces", {})
            for name, ws in workspaces.items():
                path_str = ws if isinstance(ws, str) else ws.get("path", "")
                if not path_str:
                    continue
                repo_path = Path(path_str).expanduser().resolve()
                lock_path = repo_path / tools.CM_DIR / "lock.json"
                if lock_path.exists():
                    lock = tools._atomic_json_read(lock_path)
                    if lock and lock.get("session_phase") != "done":
                        if target_path is not None:
                            session_wt = lock.get("session_worktree", "")
                            lock_targets = [repo_path]
                            if session_wt:
                                lock_targets.append(Path(session_wt).expanduser().resolve())
                            if not any(
                                target_path == candidate or candidate in target_path.parents
                                for candidate in lock_targets
                            ):
                                continue
                        lock["_repo_path"] = str(repo_path)
                        return lock
        except Exception as exc:
            logger.debug("Failed to find active lock: %s", exc)
        return None

    def _resolve_session_cwd(self) -> str | None:
        """Find current session worktree or repo path from lock state."""
        lock = self._find_active_lock()
        if lock:
            wt = lock.get("session_worktree", "")
            if wt and Path(wt).exists():
                return wt
            repo_path = lock.get("_repo_path", "")
            if repo_path:
                return repo_path
        return None

    # ──────────────────────────────────────────────────────────
    #  Skillkit registration
    # ──────────────────────────────────────────────────────────

    def _createSkills(self) -> List[SkillFunction]:
        return [
            # Session lifecycle
            SkillFunction(self._cm_repos),
            SkillFunction(self._cm_start),
            SkillFunction(self._cm_lock),
            SkillFunction(self._cm_unlock),
            SkillFunction(self._cm_status),
            # Feature delivery
            SkillFunction(self._cm_claim),
            SkillFunction(self._cm_dev),
            SkillFunction(self._cm_test),
            SkillFunction(self._cm_done),
            SkillFunction(self._cm_reopen),
            SkillFunction(self._cm_integrate),
            SkillFunction(self._cm_submit),
            # Analysis / Review
            SkillFunction(self._cm_scope),
            SkillFunction(self._cm_report),
            SkillFunction(self._cm_engine_run),
            # Utility
            SkillFunction(self._cm_progress),
            SkillFunction(self._cm_journal),
            SkillFunction(self._cm_regression),
            SkillFunction(self._cm_change_summary),
            SkillFunction(self._cm_doctor),
            # File operations (v4.5)
            SkillFunction(self._cm_read),
            SkillFunction(self._cm_find),
            SkillFunction(self._cm_grep),
            SkillFunction(self._cm_edit),
            # Escape hatches
            SkillFunction(self._cm_git),
        ]
