"""Durable checkpoints for the fixed fetch/analyze/deliver routine shape."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from ..session.persistence import SessionPersistence


_STAGE_ORDER = ("fetch", "analyze")
_DELIVERY_STEPS = {"mailbox", "history", "realtime"}


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_delivery_key(execution_id: str, output: str, destination: str) -> str:
    material = f"{execution_id}\0{content_hash(output)}\0{destination}"
    return content_hash(material)


class RoutineCheckpointStore:
    """Persist stage artifacts and delivery state under the workspace runtime area."""

    def __init__(self, workspace_path: Path, execution_id: str, task_id: str):
        directory_name = content_hash(execution_id)[:24]
        self.directory = Path(workspace_path) / ".runtime" / "routine-checkpoints" / directory_name
        self.manifest_path = self.directory / "manifest.json"
        self.execution_id = execution_id
        self.task_id = task_id

    def read_manifest(self) -> dict:
        if not self.manifest_path.exists():
            return self._new_manifest()
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._new_manifest()
        if data.get("execution_id") != self.execution_id or data.get("task_id") != self.task_id:
            return self._new_manifest()
        data.setdefault("stages", {})
        return data

    async def run_stage(
        self,
        name: str,
        input_value: str,
        producer: Callable[[], Awaitable[str]],
        *,
        run_id: str,
    ) -> str:
        if name not in _STAGE_ORDER:
            raise ValueError(f"Unsupported routine stage: {name}")
        manifest = self.read_manifest()
        input_digest = content_hash(input_value)
        stage = manifest["stages"].get(name)
        if stage and stage.get("input_hash") == input_digest:
            artifact = self.directory / str(stage.get("artifact", ""))
            if artifact.is_file():
                output = artifact.read_text(encoding="utf-8")
                if content_hash(output) == stage.get("output_hash"):
                    return output

        self._invalidate_from(manifest, name)
        output = str(await producer())
        artifact_name = f"{name}.txt"
        self.directory.mkdir(parents=True, exist_ok=True)
        SessionPersistence.atomic_save(
            self.directory / artifact_name,
            output.encode("utf-8"),
        )
        manifest["stages"][name] = {
            "input_hash": input_digest,
            "output_hash": content_hash(output),
            "artifact": artifact_name,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "producing_run_id": run_id,
        }
        self._write_manifest(manifest)
        return output

    async def run_delivery_step(
        self,
        step: str,
        delivery_key: str,
        operation: Callable[[], Awaitable[object]],
    ) -> bool:
        if step not in _DELIVERY_STEPS:
            raise ValueError(f"Unsupported delivery step: {step}")
        manifest = self.read_manifest()
        delivery = manifest.get("delivery")
        if not isinstance(delivery, dict) or delivery.get("key") != delivery_key:
            delivery = {"key": delivery_key, "steps": {}}
            manifest["delivery"] = delivery
        steps = delivery.setdefault("steps", {})
        if steps.get(step) in {"pending", "delivered"}:
            return False

        steps[step] = "pending"
        self._write_manifest(manifest)
        await operation()
        steps[step] = "delivered"
        self._write_manifest(manifest)
        return True

    def _new_manifest(self) -> dict:
        return {
            "version": 1,
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "stages": {},
        }

    def _invalidate_from(self, manifest: dict, name: str) -> None:
        index = _STAGE_ORDER.index(name)
        for stage_name in _STAGE_ORDER[index:]:
            manifest["stages"].pop(stage_name, None)
        manifest.pop("delivery", None)

    def _write_manifest(self, manifest: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        SessionPersistence.atomic_save(self.manifest_path, payload)
