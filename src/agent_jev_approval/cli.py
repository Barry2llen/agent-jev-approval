"""Command-line entry point for agent permission hooks."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Sequence
from typing import TextIO

from .adapters.codex import MalformedCodexInput, parse_codex_permission_request, render_result
from .approval import ApprovalProvider, evaluate_approval
from .policy import DEFAULT_POLICY
from .providers.typesafe import TypeSafeProvider


def main(argv: Sequence[str] | None = None) -> int:
    """Run the selected adapter and preserve native approval on every fallback."""

    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["codex"]:
        print("usage: agent-jev-approval codex", file=sys.stderr)
        return 2
    return run_codex_hook(sys.stdin, sys.stdout, sys.stderr)


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


__all__ = ["main", "run_codex_hook"]
