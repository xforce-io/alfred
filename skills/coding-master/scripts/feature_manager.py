#!/usr/bin/env python3
"""Feature Plan management — task splitting for multi-step work."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ARTIFACT_DIR = ".coding-master"
PLAN_FILENAME = "feature_plan.json"


class FeatureManager:
    def __init__(self, workspace_path: str):
        self.workspace_path = Path(workspace_path)
        self.plan_path = self.workspace_path / ARTIFACT_DIR / PLAN_FILENAME
        self.features_dir = self.workspace_path / ARTIFACT_DIR / "features"

    # ── CRUD ─────────────────────────────────────────────────

    def create_plan(self, origin_task: str, features: list[dict]) -> dict:
        """Create a feature split plan.

        features: list of {title, task, depends_on?, acceptance_criteria?}
        """
        plan = {
            "origin_task": origin_task,
            "created_at": _now_iso(),
            "features": [],
        }
        for i, f in enumerate(features):
            criteria = f.get("acceptance_criteria", [])
            plan["features"].append({
                "index": i,
                "title": f.get("title", f"Feature {i}"),
                "task": f.get("task", ""),
                "status": "pending",
                "depends_on": f.get("depends_on", []),
                "branch": None,
                "pr": None,
                "criteria_count": len(criteria),
                "verified_count": 0,
                "attempts": 0,
                "started_at": None,
                "completed_at": None,
            })
            # Write criteria file for each feature
            if criteria:
                self._ensure_feature_dir(i)
                criteria_path = self.features_dir / str(i) / "criteria.json"
                criteria_path.write_text(
                    json.dumps(criteria, indent=2, ensure_ascii=False)
                )
        self._save(plan)
        return {"ok": True, "data": {
            "total": len(plan["features"]),
            "features": [
                {"index": f["index"], "title": f["title"], "status": f["status"],
                 "criteria_count": f["criteria_count"]}
                for f in plan["features"]
            ],
        }}

    def create_plan_from_analysis(
        self, origin_task: str, features: list[dict]
    ) -> dict:
        """Thin wrapper over create_plan for analyze output.

        Accepts features list from engine output (with acceptance_criteria),
        delegates to create_plan which handles criteria file writing.
        """
        return self.create_plan(origin_task, features)

    def next_feature(self) -> dict:
        """Return the next executable feature (pending + all deps done).

        Auto-sets its status to in_progress and records started_at.
        """
        plan = self._load()
        if plan is None:
            return {"ok": False, "error": "no feature plan found"}

        features = plan["features"]
        done_indices = {f["index"] for f in features if f["status"] == "done"}

        for f in features:
            if f["status"] != "pending":
                continue
            deps = f.get("depends_on", [])
            if all(d in done_indices for d in deps):
                f["status"] = "in_progress"
                f["started_at"] = _now_iso()
                self._save(plan)
                completed = sum(1 for x in features if x["status"] == "done")
                # Load criteria summary if available
                criteria = self._load_criteria(f["index"])
                data = {
                    "feature": f,
                    "progress": f"{completed}/{len(features)}",
                }
                if criteria:
                    data["criteria_count"] = len(criteria)
                    data["criteria_types"] = list({
                        c.get("type", "manual") for c in criteria
                    })
                return {"ok": True, "data": data}

        # Check if all done or blocked
        completed = sum(1 for f in features if f["status"] in ("done", "skipped"))
        if completed == len(features):
            return {"ok": True, "data": {
                "feature": None,
                "status": "all_complete",
                "progress": f"{completed}/{len(features)}",
            }}

        return {"ok": True, "data": {
            "feature": None,
            "status": "blocked",
            "progress": f"{completed}/{len(features)}",
            "message": "remaining features are blocked by unfinished dependencies",
        }}

    def mark_done(
        self,
        index: int,
        branch: str | None = None,
        pr: str | None = None,
        force: bool = False,
    ) -> dict:
        """Mark a feature as done, record branch/pr.

        Checks auto criteria before marking done:
        - All auto criteria passed → done
        - Auto criteria not met and no force → CRITERIA_NOT_MET error
        - Auto criteria not met but force=True → done anyway
        """
        plan = self._load()
        if plan is None:
            return {"ok": False, "error": "no feature plan found"}

        if index < 0 or index >= len(plan["features"]):
            return {"ok": False, "error": f"feature index {index} out of range"}

        f = plan["features"][index]

        # Check criteria if they exist
        criteria = self._load_criteria(index)
        if criteria and not force:
            verification = self._load_verification(index)
            auto_criteria = [c for c in criteria if c.get("type") != "manual"]
            if auto_criteria:
                # Build a set of passed criteria descriptions
                passed_set = {
                    v["description"]
                    for v in verification
                    if v.get("passed") is True
                }
                not_met = [
                    c for c in auto_criteria
                    if c.get("description", "") not in passed_set
                ]
                if not_met:
                    return {
                        "ok": False,
                        "error": "acceptance criteria not met",
                        "error_code": "CRITERIA_NOT_MET",
                        "data": {
                            "not_met": not_met,
                            "hint": "use --force to skip criteria check",
                        },
                    }

        f["status"] = "done"
        f["completed_at"] = _now_iso()
        if branch:
            f["branch"] = branch
        if pr:
            f["pr"] = pr
        self._save(plan)

        completed = sum(1 for x in plan["features"] if x["status"] in ("done", "skipped"))
        remaining = len(plan["features"]) - completed

        # Note any manual criteria still pending
        pending_manual = []
        if criteria:
            pending_manual = [
                c for c in criteria if c.get("type") == "manual"
            ]

        data = {
            "completed": completed,
            "remaining": remaining,
            "marked": {"index": index, "title": f["title"]},
        }
        if pending_manual:
            data["pending_manual"] = pending_manual

        return {"ok": True, "data": data}

    def list_all(self) -> dict:
        """Return all features and status summary."""
        plan = self._load()
        if plan is None:
            return {"ok": False, "error": "no feature plan found"}

        features = plan["features"]
        return {"ok": True, "data": {
            "origin_task": plan["origin_task"],
            "total": len(features),
            "completed": sum(1 for f in features if f["status"] == "done"),
            "skipped": sum(1 for f in features if f["status"] == "skipped"),
            "features": [
                {
                    "index": f["index"],
                    "title": f["title"],
                    "status": f["status"],
                    "branch": f.get("branch"),
                    "pr": f.get("pr"),
                    "criteria_count": f.get("criteria_count", 0),
                    "verified_count": f.get("verified_count", 0),
                    "attempts": f.get("attempts", 0),
                    "started_at": f.get("started_at"),
                    "completed_at": f.get("completed_at"),
                }
                for f in features
            ],
        }}

    def update(
        self,
        index: int,
        status: str | None = None,
        title: str | None = None,
        task: str | None = None,
    ) -> dict:
        """Update a single feature (skip, modify task, etc.)."""
        plan = self._load()
        if plan is None:
            return {"ok": False, "error": "no feature plan found"}

        if index < 0 or index >= len(plan["features"]):
            return {"ok": False, "error": f"feature index {index} out of range"}

        f = plan["features"][index]
        if status:
            f["status"] = status
        if title:
            f["title"] = title
        if task:
            f["task"] = task
        self._save(plan)

        return {"ok": True, "data": {
            "updated": {"index": index, "title": f["title"], "status": f["status"]},
        }}

    # ── Acceptance Criteria ──────────────────────────────────

    def criteria(
        self,
        index: int,
        action: str = "view",
        new_criteria: list[dict] | None = None,
    ) -> dict:
        """View or append acceptance criteria for a feature.

        action=view: return criteria.json content
        action=append: append entries, update criteria_count
        """
        plan = self._load()
        if plan is None:
            return {"ok": False, "error": "no feature plan found"}

        if index < 0 or index >= len(plan["features"]):
            return {"ok": False, "error": f"feature index {index} out of range"}

        if action == "view":
            criteria = self._load_criteria(index)
            return {"ok": True, "data": {"index": index, "criteria": criteria}}

        if action == "append":
            if not new_criteria:
                return {"ok": False, "error": "no criteria provided to append"}

            existing = self._load_criteria(index)
            existing.extend(new_criteria)
            self._ensure_feature_dir(index)
            criteria_path = self.features_dir / str(index) / "criteria.json"
            criteria_path.write_text(
                json.dumps(existing, indent=2, ensure_ascii=False)
            )
            # Update criteria_count in plan
            plan["features"][index]["criteria_count"] = len(existing)
            self._save(plan)
            return {"ok": True, "data": {
                "index": index,
                "criteria_count": len(existing),
                "appended": len(new_criteria),
            }}

        return {"ok": False, "error": f"unknown action: {action}"}

    # ── Verification ─────────────────────────────────────────

    def verify(
        self,
        index: int,
        workspace: str | None = None,
        engine=None,
    ) -> dict:
        """Execute feature-level acceptance criteria verification.

        - test type: run command via subprocess, check exit code
        - assert type: call engine to check code matches description
        - manual type: mark passed=null (user must verify)

        Results written to features/{index}/verification.json.
        """
        plan = self._load()
        if plan is None:
            return {"ok": False, "error": "no feature plan found"}

        if index < 0 or index >= len(plan["features"]):
            return {"ok": False, "error": f"feature index {index} out of range"}

        criteria = self._load_criteria(index)
        if not criteria:
            return {"ok": True, "data": {
                "index": index,
                "all_auto_passed": True,
                "results": [],
                "message": "no criteria defined",
            }}

        ws = workspace or str(self.workspace_path)
        results = []

        for c in criteria:
            ctype = c.get("type", "manual")
            result = {
                "description": c.get("description", ""),
                "type": ctype,
            }

            if ctype == "test":
                command = c.get("target", c.get("command", ""))
                if not command:
                    result["passed"] = False
                    result["error"] = "no test command specified"
                else:
                    try:
                        proc = subprocess.run(
                            command, shell=True, cwd=ws,
                            capture_output=True, text=True, timeout=300,
                        )
                        result["passed"] = proc.returncode == 0
                        result["exit_code"] = proc.returncode
                        if not result["passed"]:
                            # Truncate output to avoid huge payloads
                            result["output"] = (proc.stdout + proc.stderr)[-2000:]
                    except subprocess.TimeoutExpired:
                        result["passed"] = False
                        result["error"] = "test timed out (300s)"

            elif ctype == "assert":
                if engine is None:
                    result["passed"] = None
                    result["error"] = "no engine provided for assert verification"
                else:
                    description = c.get("description", "")
                    try:
                        eng_result = engine.run(
                            ws,
                            f"Check if the following requirement is satisfied in the codebase. "
                            f"Answer ONLY 'PASS' or 'FAIL' followed by a brief reason.\n\n"
                            f"Requirement: {description}",
                            max_turns=5,
                        )
                        summary = eng_result.summary.strip().upper()
                        result["passed"] = summary.startswith("PASS")
                        result["detail"] = eng_result.summary
                    except Exception as e:
                        result["passed"] = False
                        result["error"] = str(e)

            else:  # manual
                result["passed"] = None
                result["note"] = "requires manual verification"

            results.append(result)

        # Write verification results
        self._ensure_feature_dir(index)
        verification_path = self.features_dir / str(index) / "verification.json"
        verification_path.write_text(
            json.dumps(results, indent=2, ensure_ascii=False)
        )

        # Update plan counters
        auto_results = [r for r in results if r["type"] != "manual"]
        verified = sum(1 for r in auto_results if r.get("passed") is True)
        plan["features"][index]["verified_count"] = verified
        plan["features"][index]["attempts"] = plan["features"][index].get("attempts", 0) + 1
        self._save(plan)

        all_auto_passed = all(
            r.get("passed") is True for r in auto_results
        ) if auto_results else True

        pending_manual = [r for r in results if r["type"] == "manual"]

        return {"ok": True, "data": {
            "index": index,
            "all_auto_passed": all_auto_passed,
            "results": results,
            **({"pending_manual": pending_manual} if pending_manual else {}),
        }}

    # ── Internal helpers ─────────────────────────────────────

    def _ensure_feature_dir(self, index: int) -> Path:
        """Ensure features/{index}/ directory exists, return path."""
        d = self.features_dir / str(index)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _load_criteria(self, index: int) -> list[dict]:
        """Load features/{index}/criteria.json, return empty list if missing."""
        p = self.features_dir / str(index) / "criteria.json"
        if p.exists():
            return json.loads(p.read_text())
        return []

    def _load_verification(self, index: int) -> list[dict]:
        """Load features/{index}/verification.json, return empty list if missing."""
        p = self.features_dir / str(index) / "verification.json"
        if p.exists():
            return json.loads(p.read_text())
        return []

    # ── Persistence ──────────────────────────────────────────

    def _load(self) -> dict | None:
        if not self.plan_path.exists():
            return None
        return json.loads(self.plan_path.read_text())

    def _save(self, plan: dict) -> None:
        self.plan_path.parent.mkdir(exist_ok=True)
        self.plan_path.write_text(
            json.dumps(plan, indent=2, ensure_ascii=False)
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
