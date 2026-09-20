"""Privacy-preserving, local audit logging for Codex Hook invocations."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .models import ApprovalDecision


AUDIT_ENVIRONMENT_VARIABLE = "AGENT_JEV_AUDIT_LOG"
DEFAULT_AUDIT_FILENAME = "agent-jev-approval.audit.jsonl"
AUDIT_SCHEMA_VERSION = 1
MAX_METADATA_LENGTH = 128
_SAFE_REASON = re.compile(r"[a-z0-9_.:-]+")


class AuditWriter(Protocol):
    """Sink for one already-sanitized audit record."""

    def write(self, record: "AuditRecord") -> None:
        """Persist one record or raise an exception to the caller."""


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """The stable, non-sensitive v1 audit event schema."""

    timestamp: str
    event_id: str
    agent: str
    action: str | None
    session_id: str | None
    turn_id: str | None
    decision: ApprovalDecision
    reason: str
    provider_called: bool
    duration_ms: int
    schema_version: int = AUDIT_SCHEMA_VERSION

    def as_dict(self) -> dict[str, object]:
        """Return the JSON-compatible representation without optional nulls."""

        record: dict[str, object] = {
            "schema_version": self.schema_version,
            "timestamp": self.timestamp,
            "event_id": self.event_id,
            "agent": _safe_identifier(self.agent) or "codex",
            "decision": self.decision.value,
            "reason": _safe_reason(self.reason),
            "provider_called": bool(self.provider_called),
            "duration_ms": max(0, int(self.duration_ms)),
        }
        for name, value in (
            ("action", self.action),
            ("session_id", self.session_id),
            ("turn_id", self.turn_id),
        ):
            safe_value = _safe_identifier(value)
            if safe_value is not None:
                record[name] = safe_value
        return record


class FileAuditWriter:
    """Append one JSONL record to a local file with a single O_APPEND write."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, record: AuditRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (
            json.dumps(record.as_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
            + b"\n"
        )
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(self.path, flags, 0o600)
        try:
            written = os.write(descriptor, encoded)
            if written != len(encoded):
                raise OSError("short audit log write")
        finally:
            os.close(descriptor)


def create_audit_writer() -> AuditWriter | None:
    """Create the configured writer, or return None for an explicit ``off``."""

    configured = os.environ.get(AUDIT_ENVIRONMENT_VARIABLE)
    if configured is not None and configured.strip().lower() == "off":
        return None
    return FileAuditWriter(resolve_audit_path(configured))


def resolve_audit_path(configured: str | None = None) -> Path:
    """Resolve a configured path relative to the Codex user directory."""

    codex_home_value = os.environ.get("CODEX_HOME", "").strip()
    codex_home = Path(codex_home_value).expanduser() if codex_home_value else Path.home() / ".codex"

    value = configured if configured is not None else os.environ.get(AUDIT_ENVIRONMENT_VARIABLE)
    value = value.strip() if value is not None else ""
    if not value or value.lower() == "off":
        path = codex_home / DEFAULT_AUDIT_FILENAME
    else:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = codex_home / path
    return path.resolve()


def new_audit_event_id() -> str:
    """Return a fresh opaque identifier for one Hook event."""

    return str(uuid.uuid4())


def utc_timestamp() -> str:
    """Return a compact RFC 3339 UTC timestamp."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def safe_identifier(value: object) -> str | None:
    """Expose only bounded, printable string metadata to the audit record."""

    return _safe_identifier(value)


def _safe_identifier(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_METADATA_LENGTH or any(not character.isprintable() for character in value):
        return None
    return value


def _safe_reason(value: object) -> str:
    if isinstance(value, str) and _SAFE_REASON.fullmatch(value):
        return value
    return "error"


__all__ = [
    "AUDIT_ENVIRONMENT_VARIABLE",
    "AUDIT_SCHEMA_VERSION",
    "DEFAULT_AUDIT_FILENAME",
    "AuditRecord",
    "AuditWriter",
    "FileAuditWriter",
    "create_audit_writer",
    "new_audit_event_id",
    "resolve_audit_path",
    "safe_identifier",
    "utc_timestamp",
]
