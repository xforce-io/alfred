#!/usr/bin/env python3
"""Feature Plan management — task splitting for multi-step work."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ARTIFACT_DIR = ".coding-master"
PLAN_FILENAME = "feature_plan.json"


class FeatureManager:
    def __init__(self, workspace_path: str):
        self.workspace_path = Path(workspace_path)
        self.plan_path = self.workspace_path / ARTIFACT_DIR / PLAN_FILENAME

    # ── CRUD ─────────────────────────────────────────────────

    def create_plan(self, origin_task: str, features: list[dict]) -> dict:
        """Create a feature split plan.

        features: list of {title, task, depends_on?}
        """
        plan = {
            "origin_task": origin_task,
            "created_at": _now_iso(),
            "features": [],
        }
        for i, f in enumerate(features):
            plan["features"].append({
                "index": i,
                "title": f.get("title", f"Feature {i}"),
                "task": f.get("task", ""),
                "status": "pending",
                "depends_on": f.get("depends_on", []),
                "branch": None,
                "pr": None,
            })
        self._save(plan)
        return {"ok": True, "data": {
            "total": len(plan["features"]),
            "features": [
                {"index": f["index"], "title": f["title"], "status": f["status"]}
                for f in plan["features"]
            ],
        }}

    def next_feature(self) -> dict:
        """Return the next executable feature (pending + all deps done).

        Auto-sets its status to in_progress.
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
                self._save(plan)
                completed = sum(1 for x in features if x["status"] == "done")
                return {"ok": True, "data": {
                    "feature": f,
                    "progress": f"{completed}/{len(features)}",
                }}

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
        self, index: int, branch: str | None = None, pr: str | None = None
    ) -> dict:
        """Mark a feature as done, record branch/pr."""
        plan = self._load()
        if plan is None:
            return {"ok": False, "error": "no feature plan found"}

        if index < 0 or index >= len(plan["features"]):
            return {"ok": False, "error": f"feature index {index} out of range"}

        f = plan["features"][index]
        f["status"] = "done"
        if branch:
            f["branch"] = branch
        if pr:
            f["pr"] = pr
        self._save(plan)

        completed = sum(1 for x in plan["features"] if x["status"] in ("done", "skipped"))
        remaining = len(plan["features"]) - completed
        return {"ok": True, "data": {
            "completed": completed,
            "remaining": remaining,
            "marked": {"index": index, "title": f["title"]},
        }}

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
