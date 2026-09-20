[CmdletBinding()]
param(
    [ValidateSet("user", "project")]
    [string]$Scope = "user",
    [string]$Path,
    [string]$Command,
    [string]$PromptCommand,
    [switch]$DryRun
)

$arguments = @("-m", "agent_jev_approval.cli", "install-codex-hook", "--scope", $Scope)

if ($Path) {
    $arguments += @("--path", $Path)
}

if ($Command) {
    $arguments += @("--command", $Command)
}

if ($PromptCommand) {
    $arguments += @("--prompt-command", $PromptCommand)
}

if ($DryRun) {
    $arguments += "--dry-run"
}

& py @arguments
exit $LASTEXITCODE
