from __future__ import annotations

import pytest

from agent_jev_approval.models import ApprovalRequest
from agent_jev_approval.providers.typesafe import TypeSafeProvider, build_questions, parse_jev_response


def response_payload() -> dict[str, object]:
    return {
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
        }
    }


def test_parse_jev_response_requires_all_typed_signals() -> None:
    result = parse_jev_response(response_payload())

    assert result.safe_to_auto_approve == 0.99
    assert result.risk_band == "read_only"
    assert result.risk_band_probability == 0.99
    assert result.risk_band_confidence == 0.95

    broken = response_payload()
    del broken["answers"]["risk_band"]  # type: ignore[index]
    with pytest.raises(ValueError):
        parse_jev_response(broken)


def test_provider_uses_one_request_with_expected_questions_and_state() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.state: object | None = None
            self.questions: object | None = None

        def system_one(self, **kwargs: object) -> dict[str, object]:
            self.state = kwargs["state"]
            self.questions = kwargs["questions"]
            return response_payload()

    client = FakeClient()
    provider = TypeSafeProvider(client=client)
    request = ApprovalRequest(
        agent="codex",
        action="Bash",
        arguments={"command": "git status"},
        cwd="C:/work",
        context={"session_id": "s1", "user_prompt": "Inspect the repository safely"},
    )

    result = provider.assess(request)

    assert result.risk_band == "read_only"
    assert client.state == {
        "agent": "codex",
        "action": "Bash",
        "arguments": {"command": "git status"},
        "cwd": "C:/work",
        "context": {"session_id": "s1", "user_prompt": "Inspect the repository safely"},
    }
    assert set(client.questions) == set(build_questions())  # type: ignore[arg-type]
