from agent_jev_approval.models import ApprovalRequest
from agent_jev_approval.policy import hard_rule_match


def request(command: str, *, action: str = "Bash") -> ApprovalRequest:
    return ApprovalRequest(agent="codex", action=action, arguments={"command": command}, cwd="C:/work")


def test_safe_read_only_commands_do_not_match_hard_rules() -> None:
    for command in ("git status", "git diff --stat", "cat README.md", "pwd", "echo \"rm -rf /\"", "echo \"sudo\""):
        assert hard_rule_match(request(command)) is None


def test_privilege_and_destructive_commands_match() -> None:
    assert hard_rule_match(request("sudo apt update")).code == "privilege_escalation"
    assert hard_rule_match(request("rm -rf ./build")).code == "recursive_delete"
    assert hard_rule_match(request("git push origin main --force")).code == "force_push"
    assert hard_rule_match(request("git reset --hard HEAD")).code == "hard_reset"
    assert hard_rule_match(request("kubectl delete deployment api")).code == "cluster_delete"
    assert hard_rule_match(request("git clean -fdx")).code == "bulk_delete"


def test_system_permission_and_credential_mutations_match() -> None:
    assert hard_rule_match(request("chmod 600 .env")).code == "permission_change"
    assert hard_rule_match(request("systemctl enable service")).code == "system_config_change"
    assert hard_rule_match(request('powershell -Command "Remove-Item -Recurse C:\\temp"')).code == "bulk_delete"
    assert hard_rule_match(request("cmd /c rmdir /s C:\\temp")).code == "bulk_delete"
    assert hard_rule_match(request("del /s C:\\temp\\*")).code == "recursive_delete"
    assert hard_rule_match(request("docker login registry.example")).code == "credential_mutation"
    assert hard_rule_match(request("git config credential.helper store")).code == "credential_mutation"
    assert hard_rule_match(request("printf secret | tee .env")).code == "credential_mutation"


def test_non_bash_structured_destructive_action_matches() -> None:
    assert hard_rule_match(
        ApprovalRequest(
            agent="codex",
            action="mcp__filesystem__delete_file",
            arguments={"path": "notes.txt"},
            cwd="C:/work",
        )
    ).code == "irreversible_operation"


def test_recursive_delete_without_broad_target_can_reach_jev() -> None:
    assert hard_rule_match(request("rm -r ./one-file")) is None
