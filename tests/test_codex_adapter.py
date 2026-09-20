from __future__ import annotations

import io
import json

from agent_jev_approval.adapters.codex import parse_codex_permission_request, render_result
from agent_jev_approval.cli import main, run_codex_hook, run_codex_user_prompt_hook
from agent_jev_approval.models import ApprovalDecision, ApprovalResult, JevAssessment
from agent_jev_approval.prompt_context import FilePromptStore, PromptRecord


class AllowProvider:
    def __init__(self) -> None:
        self.calls = 0

    def assess(self, _: object) -> JevAssessment:
        self.calls += 1
        return JevAssessment(0.99, 0.01, 0.01, 0.01, 0.01, "read_only", 0.99, 0.95)


class SecretErrorProvider:
    def assess(self, _: object) -> JevAssessment:
        raise RuntimeError("api_key=sk-secret-token")


class MissingApiKeyProvider:
    def assess(self, _: object) -> JevAssessment:
        from typesafe_sdk import TypeSafeError

        raise TypeSafeError("No API key was provided. Pass api_key or set the TYPESAFE_API_KEY environment variable.")


class NoopAuditWriter:
    def write(self, _: object) -> None:
        return


class PromptStoreStub:
    def __init__(self) -> None:
        self.prompts: dict[tuple[str, str], str] = {("session-1", "turn-1"): "Inspect the repository safely"}

    def save(self, record: PromptRecord) -> None:
        self.prompts[(record.session_id, record.turn_id)] = record.prompt

    def load(self, *, session_id: str, turn_id: str) -> str | None:
        return self.prompts.get((session_id, turn_id))


class ContextProvider:
    def __init__(self) -> None:
        self.request: object | None = None

    def assess(self, request: object) -> JevAssessment:
        self.request = request
        return JevAssessment(0.99, 0.01, 0.01, 0.01, 0.01, "read_only", 0.99, 0.95)


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

    exit_code = run_codex_hook(
        stdin,
        stdout,
        stderr,
        provider=provider,
        audit_writer=NoopAuditWriter(),
        prompt_store=PromptStoreStub(),
    )

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

    exit_code = run_codex_hook(
        stdin,
        stdout,
        stderr,
        provider=provider,
        audit_writer=NoopAuditWriter(),
        prompt_store=PromptStoreStub(),
    )

    assert exit_code == 0
    assert stdout.getvalue() == ""
    assert "hard_rule:hard_reset" in stderr.getvalue()
    assert provider.calls == 0


def test_malformed_input_falls_back_without_secret_logging() -> None:
    stdin = io.StringIO('{"hook_event_name":"PermissionRequest","tool_name":"Bash","tool_input":null}')
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = run_codex_hook(
        stdin,
        stdout,
        stderr,
        audit_writer=NoopAuditWriter(),
        prompt_store=PromptStoreStub(),
    )

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
        prompt_store=PromptStoreStub(),
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
        prompt_store=PromptStoreStub(),
    )

    assert exit_code == 0
    assert stdout.getvalue() == ""
    assert "sk-secret-token" not in stderr.getvalue()
    assert "provider:error" in stderr.getvalue()


def test_missing_api_key_provider_exception_is_diagnosed_without_text() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = run_codex_hook(
        io.StringIO(json.dumps(payload())),
        stdout,
        stderr,
        provider=MissingApiKeyProvider(),
        audit_writer=NoopAuditWriter(),
        prompt_store=PromptStoreStub(),
    )

    assert exit_code == 0
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "agent-jev-approval: fallback: provider:missing_api_key\n"


def test_user_prompt_hook_stores_prompt_without_stdout() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    store = PromptStoreStub()

    exit_code = run_codex_user_prompt_hook(
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session-1",
                    "turn_id": "turn-2",
                    "prompt": "Please inspect the current changes",
                }
            )
        ),
        stdout,
        stderr,
        prompt_store=store,
    )

    assert exit_code == 0
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == ""
    assert store.load(session_id="session-1", turn_id="turn-2") == "Please inspect the current changes"


def test_permission_request_adds_current_prompt_to_provider_context() -> None:
    provider = ContextProvider()
    stdout = io.StringIO()
    stderr = io.StringIO()

    run_codex_hook(
        io.StringIO(json.dumps(payload())),
        stdout,
        stderr,
        provider=provider,
        audit_writer=NoopAuditWriter(),
        prompt_store=PromptStoreStub(),
    )

    assert json.loads(stdout.getvalue())["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert getattr(provider.request, "context")["user_prompt"] == "Inspect the repository safely"


def test_hooks_decode_utf8_bytes_independent_of_text_stream_encoding() -> None:
    store = PromptStoreStub()
    prompt_stdout = io.StringIO()
    prompt_stderr = io.StringIO()
    prompt_payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "session-1",
        "turn_id": "turn-utf8",
        "prompt": "检查仓库并保持范围不变",
    }
    prompt_input = io.TextIOWrapper(
        io.BytesIO(json.dumps(prompt_payload, ensure_ascii=False).encode("utf-8")),
        encoding="gbk",
    )

    assert run_codex_user_prompt_hook(prompt_input, prompt_stdout, prompt_stderr, prompt_store=store) == 0
    assert prompt_stdout.getvalue() == ""
    assert prompt_stderr.getvalue() == ""
    assert store.load(session_id="session-1", turn_id="turn-utf8") == "检查仓库并保持范围不变"

    permission = payload()
    permission["turn_id"] = "turn-utf8"
    permission["tool_input"] = {"command": "git add -- README.md", "justification": "需要写入"}
    provider = ContextProvider()
    stdout = io.StringIO()
    stderr = io.StringIO()
    permission_input = io.TextIOWrapper(
        io.BytesIO(json.dumps(permission, ensure_ascii=False).encode("utf-8")),
        encoding="gbk",
    )

    assert (
        run_codex_hook(
            permission_input,
            stdout,
            stderr,
            provider=provider,
            audit_writer=NoopAuditWriter(),
            prompt_store=store,
        )
        == 0
    )
    assert json.loads(stdout.getvalue())["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert getattr(provider.request, "arguments")["justification"] == "需要写入"


def test_oversized_prompt_capture_does_not_write_stdout(tmp_path) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    store = FilePromptStore(tmp_path / "prompts", max_prompt_length=4)

    run_codex_user_prompt_hook(
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session-1",
                    "turn_id": "turn-1",
                    "prompt": "too long",
                }
            )
        ),
        stdout,
        stderr,
        prompt_store=store,
    )

    assert stdout.getvalue() == ""
    assert "prompt_capture_error" in stderr.getvalue()


def test_unknown_cli_command_is_not_a_hook_decision() -> None:
    stderr = io.StringIO()
    original_stderr = __import__("sys").stderr
    try:
        __import__("sys").stderr = stderr
        assert main(["claude"]) == 2
    finally:
        __import__("sys").stderr = original_stderr
