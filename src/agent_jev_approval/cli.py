"""Command-line entry point for agent permission hooks."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Sequence
from dataclasses import replace
from typing import TextIO

from .adapters.codex import (
    MalformedCodexInput,
    parse_codex_permission_request,
    parse_codex_user_prompt,
    render_result,
)
from .approval import ApprovalProvider, evaluate_approval
from .audit import (
    AuditRecord,
    AuditWriter,
    create_audit_writer,
    new_audit_event_id,
    safe_identifier,
    utc_timestamp,
)
from .codex_setup import (
    DEFAULT_HOOK_COMMAND,
    DEFAULT_HOOK_TIMEOUT,
    DEFAULT_PROMPT_HOOK_COMMAND,
    CodexHookInstallError,
    install_codex_hook,
)
from .models import ApprovalDecision, ApprovalRequest, ApprovalResult, JevAssessment
from .policy import DEFAULT_POLICY
from .prompt_context import PromptStore, create_prompt_store
from .providers.typesafe import TypeSafeProvider


def main(argv: Sequence[str] | None = None) -> int:
    """Run the selected adapter and preserve native approval on every fallback."""

    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["codex"]:
        return run_codex_hook(sys.stdin, sys.stdout, sys.stderr)
    if args == ["codex-user-prompt"]:
        return run_codex_user_prompt_hook(sys.stdin, sys.stdout, sys.stderr)
    if args and args[0] == "install-codex-hook":
        return run_install_codex_hook(args[1:], sys.stdout, sys.stderr)
    print("usage: agent-jev-approval codex | install-codex-hook", file=sys.stderr)
    return 2


def run_install_codex_hook(argv: Sequence[str], stdout: TextIO, stderr: TextIO) -> int:
    """Install or update the Codex Hook without overwriting other Hooks."""

    parser = argparse.ArgumentParser(
        prog="agent-jev-approval install-codex-hook",
        description="Merge the TypeSafe Jev PermissionRequest Hook into Codex hooks.json.",
    )
    parser.add_argument("--scope", choices=("user", "project"), default="user")
    parser.add_argument("--path", help="Explicit hooks.json path; overrides --scope.")
    parser.add_argument("--command", default=DEFAULT_HOOK_COMMAND, help="Command Codex should execute for the Hook.")
    parser.add_argument(
        "--prompt-command",
        default=DEFAULT_PROMPT_HOOK_COMMAND,
        help="Command Codex should execute for UserPromptSubmit.",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_HOOK_TIMEOUT, help="Codex Hook timeout in whole seconds.")
    parser.add_argument("--dry-run", action="store_true", help="Show the target without writing the file.")
    try:
        options = parser.parse_args(list(argv))
    except SystemExit as error:
        return int(error.code)

    try:
        result = install_codex_hook(
            scope=options.scope,
            path=options.path,
            command=options.command,
            prompt_command=options.prompt_command,
            timeout=options.timeout,
            dry_run=options.dry_run,
        )
    except CodexHookInstallError as error:
        print(f"agent-jev-approval: cannot configure Codex Hook: {error}", file=stderr)
        return 1

    if result.dry_run:
        action = "Would update" if result.changed else "Already configured"
        print(f"{action} Codex Hook: {result.path}", file=stdout)
    elif not result.changed:
        print(f"Codex Hook already configured: {result.path}", file=stdout)
    else:
        print(f"Configured Codex Hook: {result.path}", file=stdout)
        if result.backup_path is not None:
            print(f"Backup: {result.backup_path}", file=stdout)
    return 0


def run_codex_hook(
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    *,
    provider: ApprovalProvider | None = None,
    audit_writer: AuditWriter | None = None,
    prompt_store: PromptStore | None = None,
) -> int:
    """Read one Codex event, write only an allow response, and always exit safely."""

    started = time.perf_counter()
    event_id = new_audit_event_id()
    writer = audit_writer
    audit_setup_failed = False
    if writer is None:
        try:
            writer = create_audit_writer()
        except Exception:  # noqa: BLE001 - audit setup must never change approval behavior.
            audit_setup_failed = True

    current_prompt_store = prompt_store
    prompt_store_setup_failed = False
    if current_prompt_store is None:
        try:
            current_prompt_store = create_prompt_store()
        except Exception:  # noqa: BLE001 - missing prompt must fail closed below.
            prompt_store_setup_failed = True

    request: ApprovalRequest | None = None
    tracked_provider: _TrackingProvider | None = None
    result: ApprovalResult

    try:
        raw = stdin.read()
        payload = json.loads(raw)
        request = parse_codex_permission_request(payload)
    except (json.JSONDecodeError, MalformedCodexInput, TypeError, ValueError):
        result = ApprovalResult(ApprovalDecision.FALLBACK_TO_USER, "malformed_input")
    except Exception:  # noqa: BLE001 - stdin failures must also fail to user.
        result = ApprovalResult(ApprovalDecision.FALLBACK_TO_USER, "input_error")
    else:
        user_prompt = None
        if not prompt_store_setup_failed and current_prompt_store is not None:
            session_id = request.context.get("session_id")
            turn_id = request.context.get("turn_id")
            if isinstance(session_id, str) and isinstance(turn_id, str):
                try:
                    user_prompt = current_prompt_store.load(session_id=session_id, turn_id=turn_id)
                except Exception:  # noqa: BLE001 - missing prompt fails closed.
                    user_prompt = None

        if user_prompt is None:
            result = ApprovalResult(ApprovalDecision.FALLBACK_TO_USER, "missing_user_prompt")
        else:
            request = replace(request, context={**request.context, "user_prompt": user_prompt})
            try:
                selected_provider = provider if provider is not None else TypeSafeProvider(
                    timeout_seconds=DEFAULT_POLICY.provider_timeout_seconds
                )
                tracked_provider = _TrackingProvider(selected_provider)
                result = evaluate_approval(request, tracked_provider)
            except Exception:  # noqa: BLE001 - defense in depth around the hook process.
                result = ApprovalResult(ApprovalDecision.FALLBACK_TO_USER, "approval_error")

    duration_ms = max(0, int(round((time.perf_counter() - started) * 1000)))
    _write_audit_record(
        stderr,
        writer,
        setup_failed=audit_setup_failed,
        record=AuditRecord(
            timestamp=utc_timestamp(),
            event_id=event_id,
            agent="codex",
            action=request.action if request is not None else None,
            session_id=safe_identifier(request.context.get("session_id")) if request is not None else None,
            turn_id=safe_identifier(request.context.get("turn_id")) if request is not None else None,
            decision=result.decision,
            reason=result.reason,
            provider_called=tracked_provider.called if tracked_provider is not None else False,
            duration_ms=duration_ms,
        ),
    )

    rendered = render_result(result)
    if rendered:
        stdout.write(rendered)
        stdout.flush()
    else:
        _write_fallback(stderr, result.reason)
    return 0


def run_codex_user_prompt_hook(
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    *,
    prompt_store: PromptStore | None = None,
) -> int:
    """Capture one UserPromptSubmit event without emitting model-visible stdout."""

    try:
        raw = stdin.read()
        payload = json.loads(raw)
        record = parse_codex_user_prompt(payload, stored_at=time.time())
        (prompt_store if prompt_store is not None else create_prompt_store()).save(record)
    except Exception:  # noqa: BLE001 - prompt capture must never block the user's prompt.
        _write_prompt_capture_error(stderr)
    return 0


class _TrackingProvider:
    """Track whether the approval engine crossed the Provider boundary."""

    def __init__(self, delegate: ApprovalProvider) -> None:
        self._delegate = delegate
        self.called = False

    def assess(self, request: ApprovalRequest) -> JevAssessment:
        self.called = True
        return self._delegate.assess(request)


def _write_audit_record(
    stderr: TextIO,
    writer: AuditWriter | None,
    *,
    setup_failed: bool,
    record: AuditRecord,
) -> None:
    if setup_failed:
        _write_audit_warning(stderr)
        return
    if writer is None:
        return
    try:
        writer.write(record)
    except Exception:  # noqa: BLE001 - audit is best-effort and never changes the decision.
        _write_audit_warning(stderr)


def _write_audit_warning(stderr: TextIO) -> None:
    stderr.write("agent-jev-approval: audit: audit_write_error\n")
    stderr.flush()


def _write_prompt_capture_error(stderr: TextIO) -> None:
    stderr.write("agent-jev-approval: prompt_capture_error\n")
    stderr.flush()


def _write_fallback(stderr: TextIO, reason: str) -> None:
    # `reason` is generated internally from a fixed code set; never append input or
    # exception text here because Codex hook parameters can contain secrets.
    safe_reason = reason if re.fullmatch(r"[a-z0-9_.:-]+", reason) else "error"
    stderr.write(f"agent-jev-approval: fallback: {safe_reason}\n")
    stderr.flush()


if __name__ == "__main__":  # pragma: no cover - exercised by the console script.
    raise SystemExit(main())


__all__ = ["main", "run_codex_hook", "run_codex_user_prompt_hook", "run_install_codex_hook"]
