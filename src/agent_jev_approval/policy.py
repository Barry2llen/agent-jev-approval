"""Deterministic safety rules and threshold configuration."""

from __future__ import annotations

import math
import re
import shlex
from dataclasses import dataclass
from typing import Iterable, Sequence

from .models import ApprovalRequest, JevAssessment


def _is_probability(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and 0 <= float(value) <= 1


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    """Conservative thresholds used after deterministic checks."""

    min_safe_probability: float = 0.85
    max_risk_probability: float = 0.10
    min_safe_band_probability: float = 0.85
    min_confidence: float = 0.85
    provider_timeout_seconds: float = 1.5

    def __post_init__(self) -> None:
        for name in (
            "min_safe_probability",
            "max_risk_probability",
            "min_safe_band_probability",
            "min_confidence",
        ):
            value = getattr(self, name)
            if not _is_probability(value):
                raise ValueError(f"{name} must be a finite number between 0 and 1")
        if not math.isfinite(self.provider_timeout_seconds) or self.provider_timeout_seconds <= 0:
            raise ValueError("provider_timeout_seconds must be a positive finite number")


DEFAULT_POLICY = PolicyConfig()
SAFE_RISK_BANDS = frozenset({"read_only", "reversible_change"})
RISK_SIGNAL_NAMES = (
    "destructive_or_irreversible",
    "sensitive_data",
    "scope_expansion",
    "outside_task_impact",
)


@dataclass(frozen=True, slots=True)
class HardRuleMatch:
    """A stable, non-sensitive reason code for a deterministic fallback."""

    code: str


def validate_assessment(assessment: JevAssessment) -> None:
    """Reject incomplete or out-of-range provider data before thresholding it."""

    probabilities = (
        assessment.safe_to_auto_approve,
        assessment.destructive_or_irreversible,
        assessment.sensitive_data,
        assessment.scope_expansion,
        assessment.outside_task_impact,
        assessment.risk_band_probability,
        assessment.risk_band_confidence,
    )
    if not all(_is_probability(value) for value in probabilities):
        raise ValueError("provider returned an invalid probability")
    if not isinstance(assessment.risk_band, str) or not assessment.risk_band:
        raise ValueError("provider returned an invalid risk band")


def hard_rule_match(request: ApprovalRequest) -> HardRuleMatch | None:
    """Return the first conservative hard rule match, if any.

    Shell matching is intentionally lightweight: it tokenizes command segments and
    checks command names, flags, and structured arguments. It is not a full shell
    interpreter. Ambiguous commands are left for Jev, whose result is still gated by
    the conservative thresholds in the approval engine.
    """

    if _structured_action_is_high_risk(request.action):
        return HardRuleMatch("irreversible_operation")

    command_values = _command_values(request)
    for command in command_values:
        for tokens in _tokenize_segments(command):
            match = _match_command_tokens(tokens)
            if match is not None:
                return match

    return _structured_argument_match(request.arguments)


def _command_values(request: ApprovalRequest) -> list[str]:
    values: list[str] = []
    if isinstance(request.arguments, str):
        values.append(request.arguments)
    elif isinstance(request.arguments, dict):
        for key in ("command", "cmd", "script", "operation"):
            value = request.arguments.get(key)
            if isinstance(value, str):
                values.append(value)
    return values


def _structured_action_is_high_risk(action: str) -> bool:
    parts = [part for part in re.split(r"[^a-z0-9]+", action.lower()) if part]
    high_risk = {
        "delete",
        "destroy",
        "drop",
        "purge",
        "revoke",
        "reset",
        "force",
        "shutdown",
        "terminate",
    }
    return any(part in high_risk for part in parts)


def _structured_argument_match(arguments: object) -> HardRuleMatch | None:
    if not isinstance(arguments, dict):
        return None
    for key in ("operation", "action", "method", "verb"):
        value = arguments.get(key)
        if isinstance(value, str) and value.lower() in {"delete", "destroy", "drop", "purge", "revoke", "terminate"}:
            return HardRuleMatch("irreversible_operation")
    return None


def _tokenize_segments(command: str) -> Iterable[list[str]]:
    # Keep quoted strings intact so text such as echo "rm -rf" is not treated as a command.
    segments = re.split(r"&&|\|\||[;&|\n]", command)
    for segment in segments:
        if not segment.strip():
            continue
        try:
            tokens = shlex.split(segment, posix=False)
        except ValueError:
            try:
                tokens = shlex.split(segment, posix=True)
            except ValueError:
                continue
        cleaned = [token.strip("\"'") for token in tokens if token.strip("\"'")]
        if cleaned:
            yield cleaned


def _match_command_tokens(tokens: Sequence[str]) -> HardRuleMatch | None:
    lower = [_normalize_token(token) for token in tokens]
    wrapper_match = _match_wrapped_command(tokens, lower)
    if wrapper_match is not None:
        return wrapper_match

    command_index = _command_index(lower)
    if command_index is None:
        return None
    command = lower[command_index]
    args = lower[command_index + 1 :]

    if command in {"sudo", "doas"}:
        return HardRuleMatch("privilege_escalation")

    if command in {"rm", "del", "erase"} and _is_recursive_delete(args):
        return HardRuleMatch("recursive_delete")
    if command in {"rmdir", "rd"} and _has_flag(args, {"/s", "-recurse", "--recursive", "-r"}):
        return HardRuleMatch("bulk_delete")
    if command == "find" and "-delete" in args:
        return HardRuleMatch("bulk_delete")
    if command in {"remove-item", "remove_item"} and _has_flag(args, {"-recurse", "-r", "--recurse", "--recursive"}):
        return HardRuleMatch("bulk_delete")

    if command == "git":
        if _has_subcommand(args, "push") and _has_flag(args, {"--force", "-f", "--force-with-lease"}):
            return HardRuleMatch("force_push")
        if _has_subcommand(args, "reset") and "--hard" in args:
            return HardRuleMatch("hard_reset")
        if _has_subcommand(args, "config") and any(arg.startswith("credential") for arg in args):
            return HardRuleMatch("credential_mutation")
        if _has_subcommand(args, "clean") and _has_flag(args, {"-fdx", "-xdf", "-d", "-f", "-x"}):
            return HardRuleMatch("bulk_delete")

    if command == "kubectl":
        if _has_subcommand(args, "delete"):
            return HardRuleMatch("cluster_delete")
        if _has_subcommand(args, "create") and _has_subcommand(args, "secret"):
            return HardRuleMatch("credential_mutation")
        if _has_subcommand(args, "config") and _has_subcommand(args, "set-credentials"):
            return HardRuleMatch("credential_mutation")

    if command in {"chmod", "chown", "icacls", "setfacl", "takeown", "set-acl", "set_acl"}:
        return HardRuleMatch("permission_change")

    if command in {"systemctl", "service"} and _has_any_subcommand(args, {"enable", "disable", "start", "stop", "restart", "mask", "unmask", "edit"}):
        return HardRuleMatch("system_config_change")
    if command in {"set-itemproperty", "set_itemproperty", "new-itemproperty", "new_itemproperty"}:
        return HardRuleMatch("system_config_change")
    if command in {"reg", "reg.exe"} and _has_any_subcommand(args, {"add", "delete", "import"}):
        return HardRuleMatch("system_config_change")
    if command == "sc" and _has_any_subcommand(args, {"config", "create", "delete", "start", "stop"}):
        return HardRuleMatch("system_config_change")
    if command == "launchctl" and _has_any_subcommand(args, {"load", "unload", "bootstrap", "bootout", "enable", "disable"}):
        return HardRuleMatch("system_config_change")

    if _is_credential_command(command, args):
        return HardRuleMatch("credential_mutation")
    if _is_irreversible_command(command, args):
        return HardRuleMatch("irreversible_operation")
    if _is_sensitive_write(tokens, command, args):
        return HardRuleMatch("credential_mutation")

    return None


def _match_wrapped_command(tokens: Sequence[str], lower: Sequence[str]) -> HardRuleMatch | None:
    """Inspect common Windows shell wrappers without attempting full shell parsing."""

    if not lower:
        return None
    wrapper = lower[0]
    if wrapper in {"powershell", "pwsh"}:
        for flag in ("-command", "-c"):
            if flag in lower:
                index = lower.index(flag)
                if index + 1 < len(tokens):
                    nested = tokens[index + 1].strip("\"'")
                    for nested_tokens in _tokenize_segments(nested):
                        match = _match_command_tokens(nested_tokens)
                        if match is not None:
                            return match
                break
    if wrapper == "cmd" and "/c" in lower:
        index = lower.index("/c")
        if index + 1 < len(tokens):
            return _match_command_tokens(tokens[index + 1 :])
    return None


def _normalize_token(token: str) -> str:
    normalized = token.strip("\"'").lower()
    if normalized.startswith("/") and "/" not in normalized[1:]:
        return normalized
    normalized = normalized.replace("\\", "/").rsplit("/", 1)[-1]
    if normalized.endswith(".exe"):
        normalized = normalized[:-4]
    return normalized


def _command_index(tokens: Sequence[str]) -> int | None:
    wrappers = {
        "env",
        "command",
        "timeout",
        "nohup",
        "nice",
        "call",
        "start",
        "xargs",
    }
    for index, token in enumerate(tokens):
        if not token or token.startswith("-") or ("=" in token and not token.startswith("==")):
            continue
        if token in wrappers:
            continue
        return index
    return None


def _has_flag(args: Sequence[str], flags: set[str]) -> bool:
    for arg in args:
        if arg in flags:
            return True
        if arg.startswith("-") and not arg.startswith("--"):
            compact = arg.lstrip("-")
            if "r" in compact and "f" in compact:
                return True
    return False


def _is_recursive_delete(args: Sequence[str]) -> bool:
    recursive = _has_flag(args, {"-r", "-R", "--recursive", "-recurse", "--recurse", "/s"})
    force = _has_flag(args, {"-f", "--force"})
    if recursive and (force or _has_broad_delete_target(args)):
        return True
    return False


def _has_broad_delete_target(args: Sequence[str]) -> bool:
    return any(
        arg in {"*", "./*", "./", ".", "..", "/", "~", "C:", "C:/", "C:\\"} or "*" in arg
        for arg in args
        if not arg.startswith("-")
    )


def _has_subcommand(args: Sequence[str], subcommand: str) -> bool:
    return subcommand in args


def _has_any_subcommand(args: Sequence[str], subcommands: set[str]) -> bool:
    return any(arg in subcommands for arg in args)


def _is_credential_command(command: str, args: Sequence[str]) -> bool:
    pairs = {
        ("docker", "login"),
        ("gh", "login"),
        ("gcloud", "login"),
        ("az", "login"),
        ("security", "add-generic-password"),
        ("secret-tool", "store"),
        ("pass", "insert"),
    }
    if any(command == executable and subcommand in args for executable, subcommand in pairs):
        return True
    return command == "kubectl" and _has_subcommand(args, "create") and _has_subcommand(args, "secret")


def _is_irreversible_command(command: str, args: Sequence[str]) -> bool:
    if command in {"mkfs", "format", "diskpart", "dd", "truncate"}:
        return True
    if command == "docker" and _has_any_subcommand(args, {"system", "prune"}) and "prune" in args:
        return True
    return False


def _is_sensitive_write(tokens: Sequence[str], command: str, args: Sequence[str]) -> bool:
    write_commands = {"tee", "set-content", "add-content", "out-file", "cp", "copy", "mv", "move", "install", "touch"}
    if command in write_commands and any(_looks_sensitive_path(arg) for arg in args):
        return True
    for index, token in enumerate(tokens):
        if token in {">", ">>"} and any(_looks_sensitive_path(arg) for arg in tokens[index + 1 :]):
            return True
    return bool(re.search(r"(?:>|>>)\s*[\"']?([^\s|;&\"']+)", " ".join(tokens)) and any(_looks_sensitive_path(arg) for arg in args))


def _looks_sensitive_path(value: str) -> bool:
    path = value.strip("\"'").replace("\\", "/").lower()
    if path.endswith((".example", ".sample")):
        return False
    name = path.rsplit("/", 1)[-1]
    return bool(
        name == "credentials"
        or name.startswith(".env")
        or name in {"id_rsa", "id_ed25519"}
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
        or "secret" in name
    )


__all__ = [
    "DEFAULT_POLICY",
    "HardRuleMatch",
    "PolicyConfig",
    "RISK_SIGNAL_NAMES",
    "SAFE_RISK_BANDS",
    "hard_rule_match",
    "validate_assessment",
]
