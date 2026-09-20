"""Ephemeral local storage for the current Codex user prompt."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


DEFAULT_PROMPT_STORE_DIRECTORY = "agent-jev-approval-prompts"
PROMPT_TTL_SECONDS = 60 * 60
MAX_PROMPT_LENGTH = 16 * 1024
MAX_PROMPT_IDENTIFIER_LENGTH = 256


class PromptStoreError(RuntimeError):
    """Raised when a prompt cannot be safely stored."""


class PromptTooLongError(PromptStoreError):
    """Raised when a user prompt exceeds the bounded local cache size."""


@dataclass(frozen=True, slots=True)
class PromptRecord:
    """One prompt correlated to a Codex session and turn."""

    session_id: str
    turn_id: str
    prompt: str
    stored_at: float


class PromptStore(Protocol):
    """Storage seam used by the capture and PermissionRequest hooks."""

    def save(self, record: PromptRecord) -> None:
        """Persist the latest prompt for its session and turn."""

    def load(self, *, session_id: str, turn_id: str) -> str | None:
        """Return a non-expired prompt for the exact session and turn."""


class FilePromptStore:
    """Store bounded prompts in private, atomically replaced JSON files."""

    def __init__(
        self,
        root: Path,
        *,
        ttl_seconds: int = PROMPT_TTL_SECONDS,
        max_prompt_length: int = MAX_PROMPT_LENGTH,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_prompt_length <= 0:
            raise ValueError("max_prompt_length must be positive")
        self.root = Path(root)
        self.ttl_seconds = ttl_seconds
        self.max_prompt_length = max_prompt_length
        self._clock = clock

    def save(self, record: PromptRecord) -> None:
        _validate_identifier(record.session_id, "session_id")
        _validate_identifier(record.turn_id, "turn_id")
        if not isinstance(record.prompt, str) or not record.prompt:
            raise PromptStoreError("prompt must be a non-empty string")
        if _prompt_size(record.prompt) > self.max_prompt_length:
            raise PromptTooLongError("prompt exceeds the local cache limit")
        if not isinstance(record.stored_at, (int, float)) or not self._is_finite(record.stored_at):
            raise PromptStoreError("stored_at must be a finite timestamp")

        self._ensure_root()
        self._prune_expired()
        session_directory = self._session_directory(record.session_id)
        session_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self._record_path(record.session_id, record.turn_id)
        for candidate in session_directory.glob("*.json"):
            if candidate != target:
                candidate.unlink(missing_ok=True)

        payload = {
            "session_id": record.session_id,
            "turn_id": record.turn_id,
            "prompt": record.prompt,
            "stored_at": float(record.stored_at),
        }
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=session_directory,
                prefix=".prompt-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, target)
        except OSError as error:
            raise PromptStoreError("cannot write prompt context") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def load(self, *, session_id: str, turn_id: str) -> str | None:
        try:
            _validate_identifier(session_id, "session_id")
            _validate_identifier(turn_id, "turn_id")
            self._prune_expired()
            path = self._record_path(session_id, turn_id)
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            if payload.get("session_id") != session_id or payload.get("turn_id") != turn_id:
                return None
            prompt = payload.get("prompt")
            stored_at = payload.get("stored_at")
            if not isinstance(prompt, str) or not prompt or _prompt_size(prompt) > self.max_prompt_length:
                return None
            if not isinstance(stored_at, (int, float)) or not self._is_finite(stored_at):
                return None
            age = self._clock() - float(stored_at)
            if age < 0 or age > self.ttl_seconds:
                path.unlink(missing_ok=True)
                return None
            return prompt
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _prune_expired(self) -> None:
        if not self.root.exists():
            return
        now = self._clock()
        for candidate in self.root.glob("*/*.json"):
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
                stored_at = payload.get("stored_at") if isinstance(payload, dict) else None
                if not isinstance(stored_at, (int, float)) or now - float(stored_at) > self.ttl_seconds:
                    candidate.unlink(missing_ok=True)
            except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
                candidate.unlink(missing_ok=True)

    def _session_directory(self, session_id: str) -> Path:
        return self.root / _digest(session_id)

    def _record_path(self, session_id: str, turn_id: str) -> Path:
        return self._session_directory(session_id) / f"{_digest(session_id + chr(0) + turn_id)}.json"

    @staticmethod
    def _is_finite(value: int | float) -> bool:
        return value == value and value not in {float("inf"), float("-inf")}


def create_prompt_store() -> PromptStore:
    """Create the default prompt store under the active Codex home."""

    codex_home_value = os.environ.get("CODEX_HOME", "").strip()
    codex_home = Path(codex_home_value).expanduser() if codex_home_value else Path.home() / ".codex"
    return FilePromptStore(codex_home / DEFAULT_PROMPT_STORE_DIRECTORY)


def _validate_identifier(value: object, name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_PROMPT_IDENTIFIER_LENGTH
        or any(not character.isprintable() for character in value)
    ):
        raise PromptStoreError(f"{name} must be a bounded printable string")


def _digest(value: str) -> str:
    # A 128-bit filename hash keeps Windows paths short while remaining opaque
    # and collision-resistant for this small local cache.
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _prompt_size(prompt: str) -> int:
    return len(prompt.encode("utf-8"))


__all__ = [
    "DEFAULT_PROMPT_STORE_DIRECTORY",
    "MAX_PROMPT_IDENTIFIER_LENGTH",
    "MAX_PROMPT_LENGTH",
    "PROMPT_TTL_SECONDS",
    "FilePromptStore",
    "PromptRecord",
    "PromptStore",
    "PromptStoreError",
    "PromptTooLongError",
    "create_prompt_store",
]
