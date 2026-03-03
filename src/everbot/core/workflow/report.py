"""Workflow completion report generation (Markdown + JSON)."""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime
from typing import List, Optional

from .models import (
    PhaseTraceEntry,
    TaskSessionConfig,
    TaskSessionState,
    WorkflowReport,
)

logger = logging.getLogger(__name__)


def generate_report(
    *,
    state: TaskSessionState,
    phase_traces: List[PhaseTraceEntry],
    config: TaskSessionConfig,
    git_start_commit: Optional[str],
    project_dir: str,
) -> WorkflowReport:
    """Generate a workflow completion report."""
    # Calculate duration
    total_duration = 0.0
    if state.start_time:
        total_duration = (datetime.now() - state.start_time).total_seconds()

    # Detect modified files via git
    files_modified = _detect_files_modified(git_start_commit, project_dir)

    # Get final artifact (last non-empty artifact)
    final_artifact = ""
    for key in reversed(list(state.artifacts.keys())):
        if not key.startswith("__") and state.artifacts[key]:
            final_artifact = state.artifacts[key]
            break

    return WorkflowReport(
        session_id=state.session_id,
        workflow_name=config.name,
        status=state.status,
        total_duration_seconds=total_duration,
        total_tool_calls=state.total_tool_calls_used,
        phase_traces=phase_traces,
        files_modified=files_modified,
        final_artifact=final_artifact,
        error="" if state.status == "done" else f"Workflow ended with status: {state.status}",
    )


def render_report_markdown(report: WorkflowReport) -> str:
    """Render a WorkflowReport as human-readable Markdown."""
    lines: List[str] = []
    status_icon = {"done": "OK", "failed": "FAILED", "cancelled": "CANCELLED"}.get(
        report.status, report.status.upper()
    )

    lines.append(f"# Workflow Report: {report.workflow_name}")
    lines.append("")
    lines.append(f"**Status**: {status_icon}")
    lines.append(f"**Session**: `{report.session_id}`")
    lines.append(f"**Duration**: {report.total_duration_seconds:.1f}s")
    lines.append(f"**Tool calls**: {report.total_tool_calls}")
    lines.append("")

    # Phase traces
    if report.phase_traces:
        lines.append("## Phase Traces")
        lines.append("")
        for trace in report.phase_traces:
            icon = "+" if trace.status == "completed" else "-"
            lines.append(
                f"- [{icon}] **{trace.phase_name}** ({trace.phase_type}): "
                f"{trace.status}"
            )
            if trace.iterations:
                lines.append(f"  - Iterations: {trace.iterations}")
            if trace.verify_traces:
                for vt in trace.verify_traces:
                    v_icon = "PASS" if vt.passed else "FAIL"
                    lines.append(
                        f"  - Verify #{vt.iteration}: {v_icon} "
                        f"({vt.duration_seconds:.1f}s)"
                    )
            if trace.rollback_triggered:
                lines.append("  - Rollback triggered")
            if trace.artifact_preview:
                preview = trace.artifact_preview[:200]
                lines.append(f"  - Artifact: {preview}...")
        lines.append("")

    # Files modified
    if report.files_modified:
        lines.append("## Files Modified")
        lines.append("")
        for f in report.files_modified:
            lines.append(f"- {f}")
        lines.append("")

    # Error
    if report.error and report.status != "done":
        lines.append("## Error")
        lines.append("")
        lines.append(f"```\n{report.error}\n```")
        lines.append("")

    return "\n".join(lines)


def render_report_json(report: WorkflowReport) -> str:
    """Render a WorkflowReport as JSON."""
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False)


def _detect_files_modified(
    start_commit: Optional[str], project_dir: str
) -> List[str]:
    """Detect files modified since start_commit using git."""
    if not start_commit:
        return []
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", start_commit, "HEAD"],
            capture_output=True,
            text=True,
            cwd=project_dir,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return [
                f.strip()
                for f in result.stdout.strip().split("\n")
                if f.strip()
            ]
    except Exception as e:
        logger.warning(
            "workflow.report.git_diff_failed",
            extra={"error": str(e)},
        )
    return []
