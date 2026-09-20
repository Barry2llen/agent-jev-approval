from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _hook_payload(command: str) -> dict[str, object]:
    return {
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": "C:/work",
        "session_id": "cli-session",
        "turn_id": "cli-turn",
    }


class _TypeSafeStubHandler(BaseHTTPRequestHandler):
    request_count = 0
    last_request_body: dict[str, object] | None = None

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API.
        type(self).request_count += 1
        request_body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        type(self).last_request_body = json.loads(request_body.decode("utf-8"))
        body = {
            "model": "jev-test",
            "answers": {
                "safe_to_auto_approve": {"type": "noul", "noul": 0.99},
                "destructive_or_irreversible": {"type": "noul", "noul": 0.01},
                "sensitive_data": {"type": "noul", "noul": 0.01},
                "scope_expansion": {"type": "noul", "noul": 0.01},
                "outside_task_impact": {"type": "noul", "noul": 0.01},
                "risk_band": {
                    "type": "choice",
                    "choice": "read_only",
                    "confidence": 0.95,
                    "probabilities": {"read_only": 0.99, "reversible_change": 0.01},
                },
            },
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_: object) -> None:
        return


def _run_cli(
    payload: dict[str, object],
    base_url: str,
    *,
    audit_path: str = "off",
    codex_home: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "TYPESAFE_API_KEY": "test-key",
            "TYPESAFE_BASE_URL": base_url,
            "TYPESAFE_LOG_LEVEL": "off",
            "AGENT_JEV_AUDIT_LOG": audit_path,
            "CODEX_HOME": codex_home,
        }
    )
    prompt_payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": payload["session_id"],
        "turn_id": payload["turn_id"],
        "prompt": "Inspect the repository and keep the change focused.",
    }
    capture = subprocess.run(
        [sys.executable, "-m", "agent_jev_approval.cli", "codex-user-prompt"],
        input=json.dumps(prompt_payload),
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )
    assert capture.returncode == 0
    assert capture.stdout == ""
    return subprocess.run(
        [sys.executable, "-m", "agent_jev_approval.cli", "codex"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )


def test_real_cli_process_emits_allow_for_stubbed_jev_response(tmp_path: Path) -> None:
    _TypeSafeStubHandler.request_count = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TypeSafeStubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = _run_cli(
            _hook_payload("git status"),
            f"http://127.0.0.1:{server.server_port}",
            codex_home=str(tmp_path / "codex"),
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }
    assert _TypeSafeStubHandler.request_count == 1


def test_real_cli_process_writes_one_audit_record(tmp_path) -> None:
    _TypeSafeStubHandler.request_count = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TypeSafeStubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    audit_path = tmp_path / "approval.audit.jsonl"
    try:
        result = _run_cli(
            _hook_payload("git status"),
            f"http://127.0.0.1:{server.server_port}",
            audit_path=str(audit_path),
            codex_home=str(tmp_path / "codex"),
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert result.returncode == 0
    records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["decision"] == "ALLOW"
    assert records[0]["reason"] == "jev:thresholds_satisfied"
    assert records[0]["provider_called"] is True
    assert "git status" not in json.dumps(records[0])


def test_real_cli_process_keeps_stdout_empty_for_hard_rule(tmp_path: Path) -> None:
    result = _run_cli(
        _hook_payload("git reset --hard HEAD"),
        "http://127.0.0.1:1",
        codex_home=str(tmp_path / "codex"),
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert "hard_rule:hard_reset" in result.stderr


def test_real_cli_process_preserves_utf8_json_under_gbk_stdio(tmp_path: Path) -> None:
    _TypeSafeStubHandler.request_count = 0
    _TypeSafeStubHandler.last_request_body = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TypeSafeStubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    environment = os.environ.copy()
    environment.update(
        {
            "TYPESAFE_API_KEY": "test-key",
            "TYPESAFE_BASE_URL": f"http://127.0.0.1:{server.server_port}",
            "TYPESAFE_LOG_LEVEL": "off",
            "AGENT_JEV_AUDIT_LOG": "off",
            "CODEX_HOME": str(tmp_path / "codex"),
            "PYTHONIOENCODING": "gbk",
        }
    )
    prompt_payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "utf8-session",
        "turn_id": "utf8-turn",
        "prompt": "检查仓库并保持范围不变",
    }
    permission_payload = _hook_payload("git status")
    permission_payload["session_id"] = "utf8-session"
    permission_payload["turn_id"] = "utf8-turn"
    permission_payload["tool_input"] = {"command": "git status", "justification": "需要写入"}
    try:
        capture = subprocess.run(
            [sys.executable, "-m", "agent_jev_approval.cli", "codex-user-prompt"],
            input=json.dumps(prompt_payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            env=environment,
            check=False,
        )
        result = subprocess.run(
            [sys.executable, "-m", "agent_jev_approval.cli", "codex"],
            input=json.dumps(permission_payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            env=environment,
            check=False,
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert capture.returncode == 0
    assert capture.stdout == b""
    assert capture.stderr == b""
    assert result.returncode == 0
    assert json.loads(result.stdout.decode("utf-8"))["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert _TypeSafeStubHandler.last_request_body is not None
    state = _TypeSafeStubHandler.last_request_body["state"]
    assert state["arguments"]["justification"] == "需要写入"
    assert state["context"]["user_prompt"] == "检查仓库并保持范围不变"
