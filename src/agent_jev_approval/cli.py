"""Command-line entry point for agent permission hooks."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from typing import TextIO

from .adapters.codex import MalformedCodexInput, parse_codex_permission_request, render_result
from .approval import ApprovalProvider, evaluate_approval
from .codex_setup import (
    DEFAULT_HOOK_COMMAND,
    DEFAULT_HOOK_TIMEOUT,
    CodexHookInstallError,
    install_codex_hook,
)
from .policy import DEFAULT_POLICY
from .providers.typesafe import TypeSafeProvider


def main(argv: Sequence[str] | None = None) -> int:
    """Run the selected adapter and preserve native approval on every fallback."""

    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["codex"]:
        return run_codex_hook(sys.stdin, sys.stdout, sys.stderr)
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
) -> int:
    """Read one Codex event, write only an allow response, and always exit safely."""

    try:
        raw = stdin.read()
        payload = json.loads(raw)
        request = parse_codex_permission_request(payload)
    except (json.JSONDecodeError, MalformedCodexInput, TypeError, ValueError):
        _write_fallback(stderr, "malformed_input")
        return 0
    except Exception:  # noqa: BLE001 - stdin failures must also fail to user.
        _write_fallback(stderr, "input_error")
        return 0

    try:
        result = evaluate_approval(
            request,
            provider or TypeSafeProvider(timeout_seconds=DEFAULT_POLICY.provider_timeout_seconds),
        )
    except Exception:  # noqa: BLE001 - defense in depth around the hook process.
        _write_fallback(stderr, "approval_error")
        return 0

    rendered = render_result(result)
    if rendered:
        stdout.write(rendered)
        stdout.flush()
    else:
        _write_fallback(stderr, result.reason)
    return 0


def _write_fallback(stderr: TextIO, reason: str) -> None:
    # `reason` is generated internally from a fixed code set; never append input or
    # exception text here because Codex hook parameters can contain secrets.
    safe_reason = reason if re.fullmatch(r"[a-z0-9_.:-]+", reason) else "error"
    stderr.write(f"agent-jev-approval: fallback: {safe_reason}\n")
    stderr.flush()


if __name__ == "__main__":  # pragma: no cover - exercised by the console script.
    raise SystemExit(main())


__all__ = ["main", "run_codex_hook", "run_install_codex_hook"]
