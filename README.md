# agent-jev-approval

**A small, conservative approval gate for coding agents, powered by TypeSafe Jev.**

[简体中文](README.zh-CN.md)

`agent-jev-approval` turns an agent's permission request into a shared approval model, evaluates it with deterministic safety rules and TypeSafe Jev, and only auto-approves when every safety gate passes. Anything uncertain is handed back to the agent's native user-approval flow.

The project is intentionally small and auditable. The first adapter targets the Codex `PermissionRequest` Hook; the core approval model is independent of Codex so additional adapters can be added later without introducing a plugin framework.

## Project status

This repository contains an early MVP:

- Supported agent: Codex `PermissionRequest` Hook
- Supported provider: TypeSafe Jev through the official Python SDK
- Automatic decisions: `ALLOW` only
- Fallback behavior: preserve the agent's native user approval
- Explicitly out of scope: proactive `deny`, Claude Code, other agents, and dynamic plugin loading

## Design principles

- **Fail to the user.** Invalid input, hard-rule matches, low confidence, provider errors, and timeouts never become an automatic approval.
- **Code owns the decision.** Jev returns typed probabilities; the program combines them with thresholds instead of trusting a single model-generated verdict.
- **Small seams.** Adapters normalize agent-specific events into `ApprovalRequest`; providers return typed assessments; the approval engine owns policy.
- **Auditable defaults.** High-impact operations are rejected from automatic approval before a provider call.

## Architecture

```text
Codex PermissionRequest (stdin)
          │
          ▼
adapters/codex.py       agent event → ApprovalRequest
          │
          ▼
approval.py             hard rules → Jev → threshold decision
       ┌──┴──┐
       │     │
    ALLOW  FALLBACK_TO_USER
       │     │
       ▼     └── empty stdout → native user approval
Codex allow JSON
```

The public seam is deliberately small:

- `ApprovalRequest`: normalized agent, action, arguments, working directory, and context.
- `ApprovalProvider`: one `assess(request)` method returning typed Jev signals.
- `evaluate_approval(...)`: deterministic policy plus provider result → `ALLOW` or `FALLBACK_TO_USER`.

## Installation

Python 3.10 or newer is required.

From a checkout, install the package:

```powershell
py -m pip install .
```

For development and tests:

```powershell
py -m pip install -e ".[test]"
```

The console entry point is:

```powershell
agent-jev-approval codex
```

## Configure TypeSafe

Set an API key in the environment used by Codex:

```powershell
$env:TYPESAFE_API_KEY = "..."
```

The official SDK reads `TYPESAFE_API_KEY` and uses `jev-latest` by default. Optional overrides are supported by the SDK:

```powershell
$env:TYPESAFE_DEFAULT_MODEL = "jev-latest"
$env:TYPESAFE_BASE_URL = "https://api.typesafe.ai"
```

The hook uses a short 1.5-second provider timeout and disables SDK retries. A missing key, timeout, connection failure, API error, or invalid response falls back to the user.

Codex's user-level directory is controlled by `CODEX_HOME`. Set it before installing the Hook when Codex uses a non-default home:

```powershell
$env:CODEX_HOME = "C:\Users\you\.codex"
```

## Connect Codex

The one-command installer merges the Hook into the user-level Codex config, keeps other Hooks, and creates a backup before changing an existing file:

```powershell
agent-jev-approval install-codex-hook
```

From a source checkout on Windows, the PowerShell wrapper is equivalent:

```powershell
.\scripts\install-codex-hook.ps1
```

Useful variants:

```powershell
# Configure the current repository instead of the user profile
agent-jev-approval install-codex-hook --scope project

# Preview the target without writing anything
agent-jev-approval install-codex-hook --dry-run

# Use an explicit path or a Python module command when the console script is not on PATH
agent-jev-approval install-codex-hook --path "$env:CODEX_HOME/hooks.json" --command "py -m agent_jev_approval.cli codex"
```

The installer is idempotent. It updates an existing `agent-jev-approval codex` entry instead of adding duplicates, preserves unrelated Hook entries, rejects malformed JSON without overwriting it, and writes atomically.
The `--timeout` value is written as a whole-number second because Codex's Hook schema expects an unsigned integer.

Copy or merge [`examples/codex-hooks.json`](examples/codex-hooks.json) into one of Codex's active Hook configuration layers:

- User: `$CODEX_HOME/hooks.json` (or Codex's default home when `CODEX_HOME` is unset)
- Repository: `<repo>/.codex/hooks.json`

The example includes `commandWindows` for Windows. If the installed console script is not on `PATH`, use:

```text
py -m agent_jev_approval.cli codex
```

Codex may ask you to review unmanaged Hooks. Use `/hooks` to inspect and trust the exact Hook definition. Re-review it after changing the command or configuration.

## Decision flow

1. Codex sends one `PermissionRequest` JSON object on stdin.
2. The Codex adapter reads `tool_name`, `tool_input`, `cwd`, and useful session metadata.
3. Deterministic hard rules identify obviously dangerous operations before any Jev call.
4. TypeSafe Jev evaluates several independent signals in one request:
   - suitability for unattended execution;
   - destructive or irreversible impact;
   - credentials, secrets, tokens, or sensitive data;
   - expansion beyond the apparent user scope;
   - impact outside the current task or workspace;
   - a risk band: `read_only`, `reversible_change`, `sensitive_or_external`, or `destructive`.
5. The program applies thresholds to all required probabilities and the risk-band confidence.
6. Only `ALLOW` writes Codex's structured allow response. Every other path writes no stdout, so Codex continues its normal user approval flow.

This MVP never proactively returns `deny`.

## Deterministic hard rules

The following classes are never auto-approved:

- `sudo` and `doas`;
- recursive or broad deletion such as `rm -rf`, `find ... -delete`, `Remove-Item -Recurse`, and `rmdir /s`;
- `git push --force`, `git push --force-with-lease`, `git reset --hard`, and `git clean -fdx`;
- `kubectl delete`;
- permission changes such as `chmod`, `chown`, `icacls`, and `Set-Acl`;
- system-level changes such as mutating `systemctl`, registry, Windows Service, or `launchctl` commands;
- credential or secret writes and login operations;
- other clearly irreversible operations.

Rules use command tokens, flags, and structured arguments. They are deliberately not a full shell parser: quoted text such as `echo "rm -rf /"` is not treated as a delete command, while ambiguous operations continue through Jev and the conservative thresholds.

## Thresholds

The default `PolicyConfig` is:

```text
min_safe_probability = 0.90
max_risk_probability = 0.10
min_safe_band_probability = 0.85
min_confidence = 0.85
provider_timeout_seconds = 1.5
```

An action is auto-approved only when all of the following are true:

- `safe_to_auto_approve >= 0.90`;
- every risk Noul is `<= 0.10`;
- `risk_band` is `read_only` or `reversible_change`;
- the selected safe risk-band probability is `>= 0.85`;
- risk-band confidence is `>= 0.85`;
- no deterministic hard rule matched.

Library callers can provide a stricter policy:

```python
from agent_jev_approval.approval import evaluate_approval
from agent_jev_approval.policy import PolicyConfig

result = evaluate_approval(
    request,
    provider,
    policy=PolicyConfig(min_safe_probability=0.95),
)
```

Lowering thresholds expands autonomous execution and should be backed by local evaluation data. The CLI uses the conservative defaults.

## Security and privacy

- Any exception, timeout, API error, missing answer, or malformed response becomes `FALLBACK_TO_USER`.
- Hard rules run before Jev, so known high-impact commands do not reach the provider.
- stdout contains only the official Codex allow JSON or nothing.
- stderr contains only stable reason codes; it does not print API keys, tokens, credentials, secrets, full commands, or full arguments.
- TypeSafe SDK body logging is disabled by the provider.
- The normalized approval request is sent to TypeSafe for evaluation. Review your data-handling requirements before enabling this for sensitive repositories; this MVP does not perform outbound secret redaction.
- Treat this Hook as one layer of defense. Use least-privilege OS accounts, repository protections, and network controls for stronger enforcement.

## Development and tests

Run the complete offline test suite:

```powershell
py -m pytest -q
```

The tests use fake providers and a local HTTP stub; they do not require a real TypeSafe API key. Coverage includes:

- safe read-only and ordinary low-risk operations;
- every listed hard-rule class;
- elevated Jev risk and insufficient confidence;
- timeout, API error, and malformed provider responses;
- Codex input normalization;
- exact allow JSON and empty-stdout fallback;
- a real CLI subprocess using a local TypeSafe stub.

To simulate Codex input manually:

```powershell
@'
{
  "hook_event_name": "PermissionRequest",
  "tool_name": "Bash",
  "tool_input": { "command": "git status" },
  "cwd": "D:\\work",
  "session_id": "demo-session",
  "turn_id": "demo-turn",
  "permission_mode": "default"
}
'@ | agent-jev-approval codex
```

Without a usable API key, stdout is expected to remain empty and stderr contains a fallback reason code.

## Adding another agent adapter

Add a module under `src/agent_jev_approval/adapters/` that:

1. parses the target agent's permission event;
2. constructs `ApprovalRequest(agent, action, arguments, cwd, context)`;
3. calls `evaluate_approval`;
4. maps only `ALLOW` to the target agent's approval protocol, while mapping fallback to its native user-approval behavior.

Do not copy hard rules, TypeSafe calls, or threshold logic into a new adapter.

## Contributing

Keep changes focused and easy to review. New policy behavior should include regression tests, adapter changes should include protocol fixtures, and no change should put secrets or full tool arguments into Hook logs.

## Protocol references

- [Codex Hooks documentation](https://developers.openai.com/zh-Hans/docs/hooks)
- [Codex `PermissionRequest` output schema](https://github.com/openai/codex/blob/main/codex-rs/hooks/schema/generated/permission-request.command.output.schema.json)
- [TypeSafe Python SDK documentation](https://docs.typesafe.ai/sdk/python)
- [TypeSafe Python SDK source](https://github.com/typesafe-ai/typesafe-sdk-python)
