"""Codex PermissionRequest stdin/stdout adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..models import ApprovalDecision, ApprovalRequest, ApprovalResult, JSONValue
from ..prompt_context import PromptRecord


CODEX_HOOK_EVENT = "PermissionRequest"
CODEX_USER_PROMPT_EVENT = "UserPromptSubmit"
_CONTEXT_FIELDS = ("session_id", "turn_id", "model", "permission_mode", "transcript_path")


class MalformedCodexInput(ValueError):
    """Raised when stdin is not a usable PermissionRequest event."""


def parse_codex_permission_request(payload: object) -> ApprovalRequest:
    """Normalize one current Codex PermissionRequest event."""

    if not isinstance(payload, Mapping):
        raise MalformedCodexInput("event is not an object")
    if payload.get("hook_event_name") != CODEX_HOOK_EVENT:
        raise MalformedCodexInput("unexpected hook event")

    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise MalformedCodexInput("missing tool_name")

    if "tool_input" not in payload or payload.get("tool_input") is None:
        raise MalformedCodexInput("missing tool_input")
    tool_input = payload["tool_input"]
    if not _is_json_value(tool_input):
        raise MalformedCodexInput("tool_input is not JSON-compatible")

    if "cwd" not in payload or not isinstance(payload.get("cwd"), str):
        raise MalformedCodexInput("missing or invalid cwd")
    cwd = payload["cwd"]

    context: dict[str, JSONValue] = {}
    for field in _CONTEXT_FIELDS:
        value = payload.get(field)
        if value is not None:
            if not _is_json_value(value):
                raise MalformedCodexInput(f"invalid context field: {field}")
            context[field] = value

    if isinstance(tool_input, Mapping) and "description" in tool_input:
        description = tool_input.get("description")
        if description is not None and not isinstance(description, str):
            raise MalformedCodexInput("tool_input.description is not a string")
        if description is not None:
            context["description"] = description

    return ApprovalRequest(
        agent="codex",
        action=tool_name.strip(),
        arguments=tool_input,
        cwd=cwd,
        context=context,
    )


def parse_codex_user_prompt(payload: object, *, stored_at: float) -> PromptRecord:
    """Normalize one Codex UserPromptSubmit event for the prompt store."""

    if not isinstance(payload, Mapping):
        raise MalformedCodexInput("event is not an object")
    if payload.get("hook_event_name") != CODEX_USER_PROMPT_EVENT:
        raise MalformedCodexInput("unexpected hook event")

    session_id = payload.get("session_id")
    turn_id = payload.get("turn_id")
    prompt = payload.get("prompt")
    if not isinstance(session_id, str) or not session_id.strip():
        raise MalformedCodexInput("missing session_id")
    if not isinstance(turn_id, str) or not turn_id.strip():
        raise MalformedCodexInput("missing turn_id")
    if not isinstance(prompt, str) or not prompt:
        raise MalformedCodexInput("missing prompt")

    return PromptRecord(
        session_id=session_id.strip(),
        turn_id=turn_id.strip(),
        prompt=prompt,
        stored_at=stored_at,
    )


def allow_response() -> dict[str, Any]:
    """Return the exact structured response accepted by Codex."""

    return {
        "hookSpecificOutput": {
            "hookEventName": CODEX_HOOK_EVENT,
            "decision": {"behavior": "allow"},
        }
    }


def render_result(result: ApprovalResult) -> str:
    """Render only ALLOW; fallback deliberately renders empty stdout."""

    if result.decision is not ApprovalDecision.ALLOW:
        return ""
    # Keep the response shape stable so no internal reason or confidence can leak to Codex.
    return json.dumps(allow_response(), separators=(",", ":")) + "\n"


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return not isinstance(value, float) or value == value and abs(value) != float("inf")
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


__all__ = [
    "CODEX_HOOK_EVENT",
    "CODEX_USER_PROMPT_EVENT",
    "MalformedCodexInput",
    "allow_response",
    "parse_codex_user_prompt",
    "parse_codex_permission_request",
    "render_result",
]
