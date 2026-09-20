"""Agent-independent approval data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, TypeAlias

JSONPrimitive: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONPrimitive | list["JSONValue"] | dict[str, "JSONValue"]


class ApprovalDecision(str, Enum):
    """The only decisions exposed by the MVP."""

    ALLOW = "ALLOW"
    FALLBACK_TO_USER = "FALLBACK_TO_USER"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """A normalized request for permission to perform an agent action."""

    agent: str
    action: str
    arguments: JSONValue
    cwd: str | None = None
    context: Mapping[str, JSONValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    """The result of the approval engine's conservative decision."""

    decision: ApprovalDecision
    reason: str
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class JevAssessment:
    """The typed Jev signals used by the policy, independent of any agent."""

    safe_to_auto_approve: float
    destructive_or_irreversible: float
    sensitive_data: float
    scope_expansion: float
    outside_task_impact: float
    risk_band: str
    risk_band_probability: float
    risk_band_confidence: float


__all__ = [
    "ApprovalDecision",
    "ApprovalRequest",
    "ApprovalResult",
    "JevAssessment",
    "JSONPrimitive",
    "JSONValue",
]
