"""Integration failure classifier — categorizes integration failures and suggests repairs."""

from __future__ import annotations

import json
import re
from enum import Enum
from pathlib import Path
from typing import Any


class FailureType(Enum):
    TEST_FAILURE = "test_failure"
    MERGE_CONFLICT = "merge_conflict"
    FILESYSTEM_ERROR = "filesystem_error"
    UNKNOWN = "unknown"


# Patterns that indicate filesystem errors in error messages
_FS_ERROR_PATTERNS = [
    re.compile(r"Permission denied", re.IGNORECASE),
    re.compile(r"No space left on device", re.IGNORECASE),
    re.compile(r"Read-only file system", re.IGNORECASE),
    re.compile(r"No such file or directory", re.IGNORECASE),
    re.compile(r"Disk quota exceeded", re.IGNORECASE),
    re.compile(r"\bEACCES\b"),
    re.compile(r"\bENOSPC\b"),
    re.compile(r"\bEROFS\b"),
    re.compile(r"\bOSError\b"),
    re.compile(r"\bIOError\b"),
    re.compile(r"\bPermissionError\b"),
    re.compile(r"\bFileNotFoundError\b"),
]

_REPAIR_SUGGESTIONS: dict[FailureType, dict[str, str]] = {
    FailureType.TEST_FAILURE: {
        "title": "测试失败",
        "description": "集成后测试/lint/typecheck 未通过",
        "steps": (
            "1. 运行 `cm reopen --feature <N>` 重新打开相关 feature\n"
            "2. 查看测试输出定位失败原因\n"
            "3. 修复代码后运行 `cm test` 验证\n"
            "4. 通过后执行 `cm done` → `cm integrate` 重试集成"
        ),
    },
    FailureType.MERGE_CONFLICT: {
        "title": "合并冲突",
        "description": "Feature 分支合并时产生 Git 冲突",
        "steps": (
            "1. 运行 `cm reopen --feature <N>` 重新打开冲突的 feature\n"
            "2. 手动解决冲突文件中的标记 (<<<<<<< / =======  / >>>>>>>)\n"
            "3. 运行 `cm test` 确认修复无误\n"
            "4. 通过后执行 `cm done` → `cm integrate` 重试集成"
        ),
    },
    FailureType.FILESYSTEM_ERROR: {
        "title": "文件系统错误",
        "description": "权限不足、磁盘空间不足或路径问题",
        "steps": (
            "1. 检查磁盘空间: `df -h .`\n"
            "2. 检查文件权限: `ls -la` 相关目录\n"
            "3. 确认 worktree 路径存在且可写\n"
            "4. 解决后重新执行 `cm integrate`"
        ),
    },
    FailureType.UNKNOWN: {
        "title": "未知错误",
        "description": "无法自动分类的错误",
        "steps": (
            "1. 查看完整错误输出定位根因\n"
            "2. 运行 `cm doctor --fix` 检查环境\n"
            "3. 如问题持续，手动检查 .coding-master/evidence/ 下的报告"
        ),
    },
}


class IntegrationFailureClassifier:
    """Classifies integration failures from report data and provides repair suggestions."""

    def __init__(self, report: dict[str, Any]):
        self.report = report

    @classmethod
    def from_file(cls, report_path: Path) -> "IntegrationFailureClassifier":
        """Load classifier from an integration-report.json file."""
        data = json.loads(report_path.read_text())
        return cls(data)

    def classify(self) -> FailureType:
        """Determine failure type from the integration report.

        Returns FailureType enum value.
        """
        if self.report.get("overall") == "passed":
            raise ValueError("Report indicates success, not a failure")

        failure_type = self.report.get("failure_type", "")

        if failure_type == "merge_conflict":
            return FailureType.MERGE_CONFLICT

        if failure_type == "test_failure":
            # Check if the test output actually indicates a filesystem error
            test_output = self.report.get("test", {}).get("output", "") or ""
            if self._is_filesystem_error(test_output):
                return FailureType.FILESYSTEM_ERROR
            return FailureType.TEST_FAILURE

        # No explicit failure_type — inspect error text for clues
        error_text = self._collect_error_text()
        if self._is_filesystem_error(error_text):
            return FailureType.FILESYSTEM_ERROR

        if "CONFLICT" in error_text.upper():
            return FailureType.MERGE_CONFLICT

        return FailureType.UNKNOWN

    def get_repair_suggestion(self, failure_type: FailureType | None = None) -> dict[str, str]:
        """Return structured repair suggestion for the given (or auto-detected) failure type."""
        if failure_type is None:
            failure_type = self.classify()
        suggestion = dict(_REPAIR_SUGGESTIONS[failure_type])

        # Enrich with context from report
        if failure_type == FailureType.MERGE_CONFLICT:
            failed_branch = self.report.get("failed_branch", "")
            failed_feature = self.report.get("failed_feature", "")
            if failed_feature:
                suggestion["failed_feature"] = failed_feature
                suggestion["steps"] = suggestion["steps"].replace("<N>", str(failed_feature))
            if failed_branch:
                suggestion["failed_branch"] = failed_branch

        elif failure_type == FailureType.TEST_FAILURE:
            test_output = self.report.get("test", {}).get("output", "")
            if test_output:
                suggestion["test_output_snippet"] = test_output[:300]

        suggestion["failure_type"] = failure_type.value
        return suggestion

    def summary(self) -> dict[str, Any]:
        """Return a complete classification summary with type and suggestion."""
        ft = self.classify()
        return {
            "failure_type": ft.value,
            "suggestion": self.get_repair_suggestion(ft),
        }

    # ── internal helpers ──

    def _collect_error_text(self) -> str:
        """Gather all error-related text from the report."""
        parts = []
        if self.report.get("error"):
            parts.append(self.report["error"])
        test_output = self.report.get("test", {}).get("output", "")
        if test_output:
            parts.append(test_output)
        for mr in self.report.get("merge_results", []):
            if mr.get("error"):
                parts.append(mr["error"])
        return "\n".join(parts)

    @staticmethod
    def _is_filesystem_error(text: str) -> bool:
        """Check if error text matches filesystem error patterns."""
        return any(p.search(text) for p in _FS_ERROR_PATTERNS)
