"""Data models for Skill Lifecycle Management."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional


class VersionStatus(str, Enum):
    DRAFT = "draft"
    TESTING = "testing"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DEPRECATED = "deprecated"



@dataclass
class EvaluationSegment:
    """Resolved skill invocation with full content — used by LLM Judge."""

    skill_id: str
    skill_version: str
    triggered_at: str  # ISO 8601 timestamp
    context_before: str  # 1 turn before skill invocation
    skill_output: str  # skill response content
    context_after: str  # 1 turn after skill invocation (user reaction)
    session_id: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> EvaluationSegment:
        return cls(
            skill_id=str(data.get("skill_id", "")),
            skill_version=str(data.get("skill_version", "baseline")),
            triggered_at=str(data.get("triggered_at", "")),
            context_before=str(data.get("context_before", "")),
            skill_output=str(data.get("skill_output", "")),
            context_after=str(data.get("context_after", "")),
            session_id=str(data.get("session_id", "")),
        )

    @classmethod
    def from_json(cls, line: str) -> EvaluationSegment:
        return cls.from_dict(json.loads(line))


@dataclass
class JudgeResult:
    """LLM Judge scoring result for a single segment."""

    segment_index: int  # position in the log file
    has_critical_issue: bool
    satisfaction: float  # 0.0 - 1.0
    reason: str
    human_override: Optional[str] = None  # "accepted" if human reviewed and approved

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if self.human_override is None:
            del d["human_override"]
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> JudgeResult:
        return cls(
            segment_index=int(data.get("segment_index", 0)),
            has_critical_issue=bool(data.get("has_critical_issue", False)),
            satisfaction=float(data.get("satisfaction", 0.0)),
            reason=str(data.get("reason", "")),
            human_override=data.get("human_override"),
        )


@dataclass
class EvalReport:
    """Aggregated evaluation report for a skill version."""

    skill_id: str
    skill_version: str
    evaluated_at: str  # ISO 8601
    segment_count: int
    critical_issue_count: int
    critical_issue_rate: float
    mean_satisfaction: float
    results: list[JudgeResult] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        """No critical issues and satisfaction above minimum threshold."""
        return self.critical_issue_rate <= 0.05 and self.mean_satisfaction >= 0.6

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["results"] = [r.to_dict() for r in self.results]
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> EvalReport:
        results = [JudgeResult.from_dict(r) for r in data.get("results", [])]
        return cls(
            skill_id=str(data.get("skill_id", "")),
            skill_version=str(data.get("skill_version", "")),
            evaluated_at=str(data.get("evaluated_at", "")),
            segment_count=int(data.get("segment_count", 0)),
            critical_issue_count=int(data.get("critical_issue_count", 0)),
            critical_issue_rate=float(data.get("critical_issue_rate", 0.0)),
            mean_satisfaction=float(data.get("mean_satisfaction", 0.0)),
            results=results,
        )

    @classmethod
    def from_json(cls, text: str) -> EvalReport:
        return cls.from_dict(json.loads(text))

    @classmethod
    def build(cls, skill_id: str, skill_version: str, results: list[JudgeResult]) -> EvalReport:
        """Build report from a list of judge results."""
        # Exclude human-overridden segments from auto-rollback denominator
        auto_results = [r for r in results if r.human_override != "accepted"]
        total = len(auto_results)
        critical = sum(1 for r in auto_results if r.has_critical_issue)

        all_sat = [r.satisfaction for r in results]
        mean_sat = sum(all_sat) / len(all_sat) if all_sat else 0.0

        return cls(
            skill_id=skill_id,
            skill_version=skill_version,
            evaluated_at=datetime.now(timezone.utc).isoformat(),
            segment_count=len(results),
            critical_issue_count=critical,
            critical_issue_rate=critical / total if total > 0 else 0.0,
            mean_satisfaction=round(mean_sat, 4),
            results=results,
        )


@dataclass
class VersionMetadata:
    """Metadata for a single skill version snapshot."""

    version: str
    created_at: str  # ISO 8601
    status: VersionStatus = VersionStatus.DRAFT
    verification_phase: str = ""  # dense / medium / extended / full
    eval_summary: Optional[Dict[str, float]] = None
    suspended_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "version": self.version,
            "created_at": self.created_at,
            "status": self.status.value,
        }
        if self.verification_phase:
            d["verification_phase"] = self.verification_phase
        if self.eval_summary:
            d["eval_summary"] = self.eval_summary
        if self.suspended_reason:
            d["suspended_reason"] = self.suspended_reason
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> VersionMetadata:
        return cls(
            version=str(data.get("version", "")),
            created_at=str(data.get("created_at", "")),
            status=VersionStatus(data.get("status", "draft")),
            verification_phase=str(data.get("verification_phase", "")),
            eval_summary=data.get("eval_summary"),
            suspended_reason=str(data.get("suspended_reason", "")),
        )

    @classmethod
    def from_json(cls, text: str) -> VersionMetadata:
        return cls.from_dict(json.loads(text))


@dataclass
class CurrentPointer:
    """Points to the current and stable versions of a skill."""

    current_version: str
    stable_version: str
    repo_baseline: bool = True  # if True, stable = repo's original SKILL.md

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CurrentPointer:
        return cls(
            current_version=str(data.get("current_version", "")),
            stable_version=str(data.get("stable_version", "")),
            repo_baseline=bool(data.get("repo_baseline", True)),
        )

    @classmethod
    def from_json(cls, text: str) -> CurrentPointer:
        return cls.from_dict(json.loads(text))
