"""Agent protocol adapters."""

from .codex import (
    CODEX_HOOK_EVENT,
    allow_response,
    parse_codex_permission_request,
    render_result,
)

__all__ = [
    "CODEX_HOOK_EVENT",
    "allow_response",
    "parse_codex_permission_request",
    "render_result",
]
