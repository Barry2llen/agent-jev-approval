from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_jev_approval.codex_setup import DEFAULT_PROMPT_HOOK_COMMAND, CodexHookInstallError, install_codex_hook
from agent_jev_approval.cli import main


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_install_creates_user_config_without_backup(tmp_path: Path) -> None:
    target = tmp_path / ".codex" / "hooks.json"

    result = install_codex_hook(path=target)

    assert result.changed is True
    assert result.backup_path is None
    config = read_json(target)
    hook = config["hooks"]["PermissionRequest"][0]["hooks"][0]
    assert hook == {
        "type": "command",
        "command": "agent-jev-approval codex",
        "commandWindows": "agent-jev-approval codex",
        "timeout": 3,
        "statusMessage": "Checking approval with Jev",
    }
    prompt_hook = config["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert prompt_hook == {
        "type": "command",
        "command": DEFAULT_PROMPT_HOOK_COMMAND,
        "commandWindows": DEFAULT_PROMPT_HOOK_COMMAND,
        "timeout": 3,
        "statusMessage": "Capturing current user prompt",
    }


def test_user_scope_uses_codex_home_environment_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home = tmp_path / "custom-codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    result = install_codex_hook(scope="user")

    assert result.path == (codex_home / "hooks.json").resolve()
    assert result.path.exists()


def test_install_merges_existing_hooks_and_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "hooks.json"
    original = {
        "description": "keep me",
        "hooks": {
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "other-tool"}]}],
            "PermissionRequest": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "existing-policy"}]}
            ],
        },
    }
    target.write_text(json.dumps(original), encoding="utf-8")

    first = install_codex_hook(path=target)
    second = install_codex_hook(path=target)

    assert first.changed is True
    assert first.backup_path is not None and first.backup_path.exists()
    assert second.changed is False
    config = read_json(target)
    assert config["description"] == "keep me"
    assert len(config["hooks"]["UserPromptSubmit"]) == 2
    prompt_hooks = [
        hook
        for group in config["hooks"]["UserPromptSubmit"]
        for hook in group["hooks"]
        if hook.get("command") == DEFAULT_PROMPT_HOOK_COMMAND
    ]
    assert len(prompt_hooks) == 1
    permission_groups = config["hooks"]["PermissionRequest"]
    assert len(permission_groups) == 2
    jev_hooks = [hook for group in permission_groups for hook in group["hooks"] if hook.get("command") == "agent-jev-approval codex"]
    assert len(jev_hooks) == 1


def test_existing_python_command_is_updated_in_place(tmp_path: Path) -> None:
    target = tmp_path / "hooks.json"
    target.write_text(
        json.dumps(
            {
                "hooks": {
                    "PermissionRequest": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "py -m agent_jev_approval.cli codex",
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    install_codex_hook(path=target, timeout=4)

    config = read_json(target)
    groups = config["hooks"]["PermissionRequest"]
    assert len(groups) == 1
    assert groups[0]["matcher"] == "Bash"
    assert groups[0]["hooks"][0]["timeout"] == 4.0


def test_custom_prompt_command_is_installed_and_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "hooks.json"
    prompt_command = "py -m agent_jev_approval.cli codex-user-prompt"

    first = install_codex_hook(path=target, prompt_command=prompt_command)
    second = install_codex_hook(path=target, prompt_command=prompt_command)

    assert first.changed is True
    assert second.changed is False
    config = read_json(target)
    prompt_hooks = [
        hook
        for group in config["hooks"]["UserPromptSubmit"]
        for hook in group["hooks"]
        if hook.get("command") == prompt_command
    ]
    assert len(prompt_hooks) == 1


def test_malformed_existing_config_is_not_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "hooks.json"
    target.write_text("not json", encoding="utf-8")

    with pytest.raises(CodexHookInstallError):
        install_codex_hook(path=target)

    assert target.read_text(encoding="utf-8") == "not json"


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    target = tmp_path / "hooks.json"

    result = install_codex_hook(path=target, dry_run=True)

    assert result.dry_run is True
    assert result.changed is True
    assert not target.exists()


def test_cli_install_command_reports_target(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "hooks.json"

    assert main(["install-codex-hook", "--path", str(target)]) == 0

    captured = capsys.readouterr()
    assert "Configured Codex Hook" in captured.out
    assert str(target) in captured.out
