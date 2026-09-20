"""Safe, idempotent installation of the Codex PermissionRequest Hook."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


HookScope = Literal["user", "project"]
DEFAULT_HOOK_COMMAND = "agent-jev-approval codex"
DEFAULT_PROMPT_HOOK_COMMAND = "agent-jev-approval codex-user-prompt"
DEFAULT_HOOK_TIMEOUT = 3
HOOK_EVENT_NAME = "PermissionRequest"
PROMPT_HOOK_EVENT_NAME = "UserPromptSubmit"


class CodexHookInstallError(RuntimeError):
    """Raised when an existing Codex config cannot be safely merged."""


@dataclass(frozen=True, slots=True)
class CodexHookInstallResult:
    """A non-sensitive summary of an installation operation."""

    path: Path
    changed: bool
    backup_path: Path | None
    dry_run: bool


def install_codex_hook(
    *,
    scope: HookScope = "user",
    path: str | os.PathLike[str] | None = None,
    command: str = DEFAULT_HOOK_COMMAND,
    prompt_command: str = DEFAULT_PROMPT_HOOK_COMMAND,
    timeout: int = DEFAULT_HOOK_TIMEOUT,
    dry_run: bool = False,
) -> CodexHookInstallResult:
    """Merge approval and current-user-prompt Hooks into hooks.json.

    Existing Hook definitions are preserved. Matching commands are updated in
    place, making the operation idempotent. Existing files are backed up before
    the first write; malformed JSON is rejected without changing the file.
    """

    target = _resolve_path(scope=scope, path=path)
    if not isinstance(command, str) or not command.strip():
        raise CodexHookInstallError("Hook command must not be empty")
    if not isinstance(prompt_command, str) or not prompt_command.strip():
        raise CodexHookInstallError("Prompt Hook command must not be empty")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise CodexHookInstallError("Hook timeout must be a positive integer number of seconds")

    original = _read_config(target)
    merged, changed = _merge_config(
        original,
        command=command,
        prompt_command=prompt_command,
        timeout=timeout,
    )
    if dry_run or not changed:
        return CodexHookInstallResult(path=target, changed=changed, backup_path=None, dry_run=dry_run)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise CodexHookInstallError(f"Cannot create Codex Hook config directory: {target.parent}") from error
    had_existing = target.exists()
    backup_path = _backup_path(target)
    if had_existing:
        _copy_file(target, backup_path)
    _atomic_write_json(target, merged)
    return CodexHookInstallResult(path=target, changed=True, backup_path=backup_path if had_existing else None, dry_run=False)


def _resolve_path(*, scope: HookScope, path: str | os.PathLike[str] | None) -> Path:
    if path is not None:
        return Path(path).expanduser().resolve()
    if scope == "user":
        codex_home = os.environ.get("CODEX_HOME", "").strip()
        base = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
        return (base / "hooks.json").resolve()
    if scope == "project":
        return (Path.cwd() / ".codex" / "hooks.json").resolve()
    raise CodexHookInstallError(f"Unsupported scope: {scope}")


def _read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CodexHookInstallError(f"Cannot read Codex Hook config: {path}") from error
    if not isinstance(value, dict):
        raise CodexHookInstallError("Codex Hook config must contain a JSON object")
    return value


def _merge_config(
    config: dict[str, Any], *, command: str, prompt_command: str, timeout: float
) -> tuple[dict[str, Any], bool]:
    # Work on a JSON round-trip copy so callers cannot observe in-place changes.
    merged = json.loads(json.dumps(config))
    hooks = merged.get("hooks")
    if hooks is None:
        hooks = {}
        merged["hooks"] = hooks
    if not isinstance(hooks, dict):
        raise CodexHookInstallError("Codex config field 'hooks' must be an object")

    desired_permission_hook = {
        "type": "command",
        "command": command,
        "commandWindows": command,
        "timeout": timeout,
        "statusMessage": "Checking approval with Jev",
    }
    desired_prompt_hook = {
        "type": "command",
        "command": prompt_command,
        "commandWindows": prompt_command,
        "timeout": timeout,
        "statusMessage": "Capturing current user prompt",
    }

    changed = _merge_event_hook(
        hooks,
        event_name=HOOK_EVENT_NAME,
        desired_hook=desired_permission_hook,
        matcher=".*",
        command_matches=lambda value: _is_our_command(value, command),
    )
    changed |= _merge_event_hook(
        hooks,
        event_name=PROMPT_HOOK_EVENT_NAME,
        desired_hook=desired_prompt_hook,
        matcher=None,
        command_matches=lambda value: _is_our_prompt_command(value, prompt_command),
    )
    return merged, changed


def _merge_event_hook(
    hooks: dict[str, Any],
    *,
    event_name: str,
    desired_hook: dict[str, Any],
    matcher: str | None,
    command_matches: Callable[[object], bool],
) -> bool:
    groups = hooks.get(event_name)
    if groups is None:
        groups = []
        hooks[event_name] = groups
    if not isinstance(groups, list):
        raise CodexHookInstallError(f"Codex config field 'hooks.{event_name}' must be an array")

    for group in groups:
        if not isinstance(group, dict):
            continue
        group_hooks = group.get("hooks")
        if not isinstance(group_hooks, list):
            continue
        for index, hook in enumerate(group_hooks):
            if isinstance(hook, dict) and hook.get("type") == "command" and command_matches(hook.get("command")):
                if hook == desired_hook:
                    return False
                group_hooks[index] = desired_hook
                return True

    new_group: dict[str, Any] = {"hooks": [desired_hook]}
    if matcher is not None:
        new_group["matcher"] = matcher
    groups.append(new_group)
    return True


def _is_our_command(value: object, requested: str) -> bool:
    if not isinstance(value, str):
        return False
    known = {
        DEFAULT_HOOK_COMMAND,
        "python -m agent_jev_approval.cli codex",
        "py -m agent_jev_approval.cli codex",
        requested,
    }
    return value in known


def _is_our_prompt_command(value: object, requested: str) -> bool:
    if not isinstance(value, str):
        return False
    known = {
        DEFAULT_PROMPT_HOOK_COMMAND,
        "python -m agent_jev_approval.cli codex-user-prompt",
        "py -m agent_jev_approval.cli codex-user-prompt",
        requested,
    }
    return value in known


def _backup_path(path: Path) -> Path:
    base = path.with_name(path.name + ".bak")
    if not base.exists():
        return base
    index = 1
    while True:
        candidate = path.with_name(f"{path.name}.bak.{index}")
        if not candidate.exists():
            return candidate
        index += 1


def _copy_file(source: Path, destination: Path) -> None:
    try:
        destination.write_bytes(source.read_bytes())
    except OSError as error:
        raise CodexHookInstallError(f"Cannot back up Codex Hook config: {source}") from error


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary_path, path)
    except OSError as error:
        raise CodexHookInstallError(f"Cannot write Codex Hook config: {path}") from error
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink(missing_ok=True)


__all__ = [
    "CodexHookInstallError",
    "CodexHookInstallResult",
    "DEFAULT_HOOK_COMMAND",
    "DEFAULT_PROMPT_HOOK_COMMAND",
    "DEFAULT_HOOK_TIMEOUT",
    "install_codex_hook",
]
