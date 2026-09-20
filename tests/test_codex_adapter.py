from __future__ import annotations

import io
import json

from agent_jev_approval.adapters.codex import parse_codex_permission_request, render_result
from agent_jev_approval.cli import main, run_codex_hook
from agent_jev_approval.models import ApprovalDecision, ApprovalResult, JevAssessment


class AllowProvider:
    def __init__(self) -> None:
        self.calls = 0

    def assess(self, _: object) -> JevAssessment:
        self.calls += 1
        return JevAssessment(0.99, 0.01, 0.01, 0.01, 0.01, "read_only", 0.99, 0.95)


class SecretErrorProvider:
    def assess(self, _: object) -> JevAssessment:
        raise RuntimeError("api_key=sk-secret-token")


class NoopAuditWriter:
    def write(self, _: object) -> None:
        return


def payload(command: str = "git status") -> dict[str, object]:
    return {
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": command, "description": "Check the repository"},
        "cwd": "C:/work",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "model": "gpt-test",
        "permission_mode": "default",
        "transcript_path": "C:/tmp/transcript.jsonl",
    }


def test_codex_payload_is_normalized_with_context() -> None:
    request = parse_codex_permission_request(payload())

    assert request.agent == "codex"
    assert request.action == "Bash"
    assert request.arguments == {"command": "git status", "description": "Check the repository"}
    assert request.cwd == "C:/work"
    assert request.context["session_id"] == "session-1"
    assert request.context["description"] == "Check the repository"


def test_allow_response_matches_codex_shape() -> None:
    result = ApprovalResult(ApprovalDecision.ALLOW, "internal", 0.99)

    assert json.loads(render_result(result)) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }


def test_cli_allow_writes_only_allow_json() -> None:
    stdin = io.StringIO(json.dumps(payload()))
    stdout = io.StringIO()
    stderr = io.StringIO()
    provider = AllowProvider()

    exit_code = run_codex_hook(stdin, stdout, stderr, provider=provider, audit_writer=NoopAuditWriter())

    assert exit_code == 0
    assert json.loads(stdout.getvalue()) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }
    assert stderr.getvalue() == ""
    assert provider.calls == 1


def test_cli_fallback_keeps_native_approval_by_leaving_stdout_empty() -> None:
    stdin = io.StringIO(json.dumps(payload("git reset --hard HEAD")))
    stdout = io.StringIO()
    stderr = io.StringIO()
    provider = AllowProvider()

    exit_code = run_codex_hook(stdin, stdout, stderr, provider=provider, audit_writer=NoopAuditWriter())

    assert exit_code == 0
    assert stdout.getvalue() == ""
    assert "hard_rule:hard_reset" in stderr.getvalue()
    assert provider.calls == 0


def test_malformed_input_falls_back_without_secret_logging() -> None:
    stdin = io.StringIO('{"hook_event_name":"PermissionRequest","tool_name":"Bash","tool_input":null}')
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = run_codex_hook(stdin, stdout, stderr, audit_writer=NoopAuditWriter())

    assert exit_code == 0
    assert stdout.getvalue() == ""
    assert "malformed_input" in stderr.getvalue()
    assert "token" not in stderr.getvalue().lower()


def test_missing_cwd_is_malformed_and_cannot_auto_approve() -> None:
    malformed = payload()
    del malformed["cwd"]
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = run_codex_hook(
        io.StringIO(json.dumps(malformed)),
        stdout,
        stderr,
        provider=AllowProvider(),
        audit_writer=NoopAuditWriter(),
    )

    assert exit_code == 0
    assert stdout.getvalue() == ""
    assert "malformed_input" in stderr.getvalue()


def test_provider_exception_does_not_reach_stderr() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = run_codex_hook(
        io.StringIO(json.dumps(payload())),
        stdout,
        stderr,
        provider=SecretErrorProvider(),
        audit_writer=NoopAuditWriter(),
    )

    assert exit_code == 0
    assert stdout.getvalue() == ""
    assert "sk-secret-token" not in stderr.getvalue()
    assert "provider:error" in stderr.getvalue()


def test_unknown_cli_command_is_not_a_hook_decision() -> None:
    stderr = io.StringIO()
    original_stderr = __import__("sys").stderr
    try:
        __import__("sys").stderr = stderr
        assert main(["claude"]) == 2
    finally:
        __import__("sys").stderr = original_stderr
