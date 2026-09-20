from __future__ import annotations

import io
import json
from pathlib import Path

from agent_jev_approval.audit import (
    AUDIT_ENVIRONMENT_VARIABLE,
    DEFAULT_AUDIT_FILENAME,
    AuditRecord,
    FileAuditWriter,
    create_audit_writer,
    resolve_audit_path,
)
from agent_jev_approval.cli import run_codex_hook
from agent_jev_approval.models import ApprovalDecision, JevAssessment
from agent_jev_approval.prompt_context import PromptRecord


class AllowProvider:
    def __init__(self) -> None:
        self.calls = 0

    def assess(self, _: object) -> JevAssessment:
        self.calls += 1
        return JevAssessment(0.99, 0.01, 0.01, 0.01, 0.01, "read_only", 0.99, 0.95)


class SecretErrorProvider:
    def assess(self, _: object) -> JevAssessment:
        raise RuntimeError("api_key=sk-secret-token")


class RecordingAuditWriter:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    def write(self, record: AuditRecord) -> None:
        self.records.append(record)


class FailingAuditWriter:
    def write(self, _: AuditRecord) -> None:
        raise OSError("disk full: secret=do-not-log")


class PromptStoreStub:
    def __init__(self, prompt: str | None = "Inspect the repository safely") -> None:
        self.prompt = prompt

    def save(self, _: PromptRecord) -> None:
        return

    def load(self, *, session_id: str, turn_id: str) -> str | None:
        if (session_id, turn_id) == ("session-1", "turn-1"):
            return self.prompt
        return None


def payload(command: str = "git status") -> dict[str, object]:
    return {
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": command, "description": "Check the repository"},
        "cwd": "C:/work/private-repository",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "transcript_path": "C:/tmp/transcript.jsonl",
    }


def _run(
    payload_value: object,
    writer: RecordingAuditWriter,
    provider: object | None = None,
    *,
    prompt: str | None = "Inspect the repository safely",
) -> tuple[str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    run_codex_hook(
        io.StringIO(json.dumps(payload_value)),
        stdout,
        stderr,
        provider=provider,  # type: ignore[arg-type]
        audit_writer=writer,
        prompt_store=PromptStoreStub(prompt),
    )
    return stdout.getvalue(), stderr.getvalue()


def test_allow_audit_contains_only_safe_metadata() -> None:
    writer = RecordingAuditWriter()
    stdout, stderr = _run(payload(), writer, AllowProvider())

    assert json.loads(stdout)["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert stderr == ""
    assert len(writer.records) == 1
    record = writer.records[0].as_dict()
    assert record["schema_version"] == 1
    assert record["agent"] == "codex"
    assert record["action"] == "Bash"
    assert record["session_id"] == "session-1"
    assert record["turn_id"] == "turn-1"
    assert record["decision"] == "ALLOW"
    assert record["reason"] == "jev:thresholds_satisfied"
    assert record["provider_called"] is True
    assert isinstance(record["duration_ms"], int)
    serialized = json.dumps(record)
    for secret in ("git status", "private-repository", "transcript.jsonl", "description", "Inspect the repository safely"):
        assert secret not in serialized


def test_hard_rule_audit_records_that_provider_was_skipped() -> None:
    writer = RecordingAuditWriter()
    provider = AllowProvider()
    stdout, stderr = _run(payload("git reset --hard HEAD"), writer, provider)

    assert stdout == ""
    assert "hard_rule:hard_reset" in stderr
    assert provider.calls == 0
    assert writer.records[0].decision is ApprovalDecision.FALLBACK_TO_USER
    assert writer.records[0].reason == "hard_rule:hard_reset"
    assert writer.records[0].provider_called is False


def test_provider_error_is_audited_without_exception_text() -> None:
    writer = RecordingAuditWriter()
    stdout, stderr = _run(payload(), writer, SecretErrorProvider())

    assert stdout == ""
    assert "provider:error" in stderr
    assert writer.records[0].reason == "provider:error"
    assert writer.records[0].provider_called is True
    assert "sk-secret-token" not in json.dumps(writer.records[0].as_dict())


def test_malformed_event_is_audited_without_request_fields() -> None:
    writer = RecordingAuditWriter()
    stdout, stderr = _run({"hook_event_name": "PermissionRequest", "tool_input": None}, writer)

    assert stdout == ""
    assert "malformed_input" in stderr
    record = writer.records[0].as_dict()
    assert record["decision"] == "FALLBACK_TO_USER"
    assert record["reason"] == "malformed_input"
    assert record["provider_called"] is False
    assert "action" not in record
    assert "session_id" not in record


def test_audit_writer_failure_does_not_change_allow_result() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()

    run_codex_hook(
        io.StringIO(json.dumps(payload())),
        stdout,
        stderr,
        provider=AllowProvider(),
        audit_writer=FailingAuditWriter(),
        prompt_store=PromptStoreStub(),
    )

    assert json.loads(stdout.getvalue())["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert "audit: audit_write_error" in stderr.getvalue()
    assert "disk full" not in stderr.getvalue()


def test_missing_prompt_falls_back_without_calling_provider() -> None:
    writer = RecordingAuditWriter()
    provider = AllowProvider()
    stdout, stderr = _run(payload(), writer, provider, prompt=None)

    assert stdout == ""
    assert "missing_user_prompt" in stderr
    assert provider.calls == 0
    assert writer.records[0].reason == "missing_user_prompt"
    assert writer.records[0].provider_called is False


def test_audit_identifiers_are_bounded_and_printable() -> None:
    record = AuditRecord(
        timestamp="2026-01-01T00:00:00.000Z",
        event_id="event-1",
        agent="codex",
        action="x" * 129,
        session_id="bad\nvalue",
        turn_id="turn-1",
        decision=ApprovalDecision.ALLOW,
        reason="safe",
        provider_called=False,
        duration_ms=-4,
    ).as_dict()

    assert "action" not in record
    assert "session_id" not in record
    assert record["turn_id"] == "turn-1"
    assert record["duration_ms"] == 0


def test_file_writer_appends_jsonl_and_resolves_configured_paths(tmp_path: Path, monkeypatch) -> None:
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv(AUDIT_ENVIRONMENT_VARIABLE, "audit/events.jsonl")

    assert resolve_audit_path() == (codex_home / "audit/events.jsonl").resolve()
    writer = FileAuditWriter(resolve_audit_path())
    writer.write(
        AuditRecord(
            timestamp="2026-01-01T00:00:00.000Z",
            event_id="event-1",
            agent="codex",
            action="Bash",
            session_id=None,
            turn_id=None,
            decision=ApprovalDecision.FALLBACK_TO_USER,
            reason="malformed_input",
            provider_called=False,
            duration_ms=1,
        )
    )

    lines = (codex_home / "audit/events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event_id"] == "event-1"


def test_audit_off_disables_writer_and_blank_uses_default(monkeypatch, tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv(AUDIT_ENVIRONMENT_VARIABLE, "off")
    assert create_audit_writer() is None

    stdout = io.StringIO()
    stderr = io.StringIO()
    run_codex_hook(
        io.StringIO(json.dumps(payload())),
        stdout,
        stderr,
        provider=AllowProvider(),
        prompt_store=PromptStoreStub(),
    )
    assert json.loads(stdout.getvalue())["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert stderr.getvalue() == ""
    assert not (codex_home / DEFAULT_AUDIT_FILENAME).exists()

    monkeypatch.setenv(AUDIT_ENVIRONMENT_VARIABLE, "")
    assert resolve_audit_path() == (codex_home / DEFAULT_AUDIT_FILENAME).resolve()


def test_invalid_audit_path_warns_without_changing_decision(monkeypatch, tmp_path: Path) -> None:
    invalid_path = tmp_path / "audit-directory"
    invalid_path.mkdir()
    monkeypatch.setenv(AUDIT_ENVIRONMENT_VARIABLE, str(invalid_path))
    stdout = io.StringIO()
    stderr = io.StringIO()

    run_codex_hook(
        io.StringIO(json.dumps(payload())),
        stdout,
        stderr,
        provider=AllowProvider(),
        prompt_store=PromptStoreStub(),
    )

    assert json.loads(stdout.getvalue())["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert "audit: audit_write_error" in stderr.getvalue()
