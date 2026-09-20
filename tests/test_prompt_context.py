from __future__ import annotations

from pathlib import Path

import pytest

from agent_jev_approval.adapters.codex import MalformedCodexInput, parse_codex_user_prompt
from agent_jev_approval.prompt_context import (
    FilePromptStore,
    PromptRecord,
    PromptTooLongError,
)


def test_user_prompt_payload_is_normalized() -> None:
    record = parse_codex_user_prompt(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "prompt": "Please inspect the repository",
        },
        stored_at=100.0,
    )

    assert record == PromptRecord("session-1", "turn-1", "Please inspect the repository", 100.0)


def test_user_prompt_payload_requires_prompt_correlation_fields() -> None:
    for payload in (
        {"hook_event_name": "UserPromptSubmit", "turn_id": "turn-1", "prompt": "x"},
        {"hook_event_name": "UserPromptSubmit", "session_id": "session-1", "prompt": "x"},
        {"hook_event_name": "UserPromptSubmit", "session_id": "session-1", "turn_id": "turn-1"},
    ):
        with pytest.raises(MalformedCodexInput):
            parse_codex_user_prompt(payload, stored_at=100.0)


def test_file_prompt_store_reuses_current_turn_and_replaces_old_turn(tmp_path: Path) -> None:
    clock = [1000.0]
    store = FilePromptStore(tmp_path / "prompts", clock=lambda: clock[0])

    store.save(PromptRecord("session-1", "turn-1", "first prompt", 1000.0))
    assert store.load(session_id="session-1", turn_id="turn-1") == "first prompt"

    store.save(PromptRecord("session-1", "turn-2", "second prompt", 1000.0))
    assert store.load(session_id="session-1", turn_id="turn-1") is None
    assert store.load(session_id="session-1", turn_id="turn-2") == "second prompt"

    paths = list((tmp_path / "prompts").rglob("*.json"))
    assert len(paths) == 1
    assert "session-1" not in str(paths[0])


def test_file_prompt_store_expires_records(tmp_path: Path) -> None:
    clock = [1000.0]
    store = FilePromptStore(tmp_path / "prompts", ttl_seconds=60, clock=lambda: clock[0])
    store.save(PromptRecord("session-1", "turn-1", "prompt", 1000.0))

    clock[0] = 1061.0
    assert store.load(session_id="session-1", turn_id="turn-1") is None
    assert not list((tmp_path / "prompts").rglob("*.json"))


def test_file_prompt_store_rejects_oversized_prompt(tmp_path: Path) -> None:
    store = FilePromptStore(tmp_path / "prompts", max_prompt_length=4)

    with pytest.raises(PromptTooLongError):
        store.save(PromptRecord("session-1", "turn-1", "12345", 1000.0))
