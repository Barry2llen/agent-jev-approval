from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _hook_payload(command: str) -> dict[str, object]:
    return {
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": "C:/work",
    }


class _TypeSafeStubHandler(BaseHTTPRequestHandler):
    request_count = 0

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API.
        type(self).request_count += 1
        _ = self.rfile.read(int(self.headers.get("Content-Length", "0")))
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
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "TYPESAFE_API_KEY": "test-key",
            "TYPESAFE_BASE_URL": base_url,
            "TYPESAFE_LOG_LEVEL": "off",
            "AGENT_JEV_AUDIT_LOG": audit_path,
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "agent_jev_approval.cli", "codex"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )


def test_real_cli_process_emits_allow_for_stubbed_jev_response() -> None:
    _TypeSafeStubHandler.request_count = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TypeSafeStubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = _run_cli(_hook_payload("git status"), f"http://127.0.0.1:{server.server_port}")
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


def test_real_cli_process_keeps_stdout_empty_for_hard_rule() -> None:
    result = _run_cli(_hook_payload("git reset --hard HEAD"), "http://127.0.0.1:1")

    assert result.returncode == 0
    assert result.stdout == ""
    assert "hard_rule:hard_reset" in result.stderr
