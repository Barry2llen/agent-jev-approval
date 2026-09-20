"""Agent protocol adapters."""

from .codex import (
    CODEX_HOOK_EVENT,
    CODEX_USER_PROMPT_EVENT,
    allow_response,
    parse_codex_permission_request,
    parse_codex_user_prompt,
    render_result,
)

__all__ = [
    "CODEX_HOOK_EVENT",
    "CODEX_USER_PROMPT_EVENT",
    "allow_response",
    "parse_codex_permission_request",
    "parse_codex_user_prompt",
    "render_result",
]
