from __future__ import annotations

import math

from agent_jev_approval.approval import evaluate_approval
from agent_jev_approval.models import ApprovalDecision, ApprovalRequest, JevAssessment


def assessment(**overrides: object) -> JevAssessment:
    values: dict[str, object] = {
        "safe_to_auto_approve": 0.99,
        "destructive_or_irreversible": 0.01,
        "sensitive_data": 0.01,
        "scope_expansion": 0.01,
        "outside_task_impact": 0.01,
        "risk_band": "read_only",
        "risk_band_probability": 0.99,
        "risk_band_confidence": 0.95,
    }
    values.update(overrides)
    return JevAssessment(**values)  # type: ignore[arg-type]


def request(command: str = "git status") -> ApprovalRequest:
    return ApprovalRequest(agent="codex", action="Bash", arguments={"command": command}, cwd="C:/work")


class StubProvider:
    def __init__(self, result: JevAssessment | Exception) -> None:
        self.result = result
        self.calls = 0

    def assess(self, _: ApprovalRequest) -> JevAssessment:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_safe_read_only_operation_is_allowed() -> None:
    provider = StubProvider(assessment())

    result = evaluate_approval(request(), provider)

    assert result.decision is ApprovalDecision.ALLOW
    assert result.reason == "jev:thresholds_satisfied"
    assert result.confidence == 0.95
    assert provider.calls == 1


def test_safe_probability_below_new_threshold_falls_back() -> None:
    provider = StubProvider(assessment(safe_to_auto_approve=0.849))

    result = evaluate_approval(request(), provider)

    assert result.decision is ApprovalDecision.FALLBACK_TO_USER
    assert result.reason == "jev:unsafe_to_auto_approve"


def test_safe_probability_at_new_threshold_is_allowed() -> None:
    provider = StubProvider(assessment(safe_to_auto_approve=0.85))

    result = evaluate_approval(request(), provider)

    assert result.decision is ApprovalDecision.ALLOW
    assert result.reason == "jev:thresholds_satisfied"


def test_regular_low_risk_operation_is_judged_by_provider() -> None:
    provider = StubProvider(assessment(risk_band="reversible_change"))

    result = evaluate_approval(request("python -m pytest tests"), provider)

    assert result.decision is ApprovalDecision.ALLOW
    assert provider.calls == 1


def test_hard_rule_falls_back_without_calling_provider() -> None:
    provider = StubProvider(assessment())

    result = evaluate_approval(request("git reset --hard HEAD"), provider)

    assert result.decision is ApprovalDecision.FALLBACK_TO_USER
    assert result.reason == "hard_rule:hard_reset"
    assert result.confidence is None
    assert provider.calls == 0


def test_high_jev_risk_falls_back() -> None:
    provider = StubProvider(assessment(destructive_or_irreversible=0.25))

    result = evaluate_approval(request(), provider)

    assert result.decision is ApprovalDecision.FALLBACK_TO_USER
    assert result.reason == "jev:risk_destructive_or_irreversible"


def test_low_confidence_falls_back() -> None:
    provider = StubProvider(assessment(risk_band_confidence=0.70))

    result = evaluate_approval(request(), provider)

    assert result.decision is ApprovalDecision.FALLBACK_TO_USER
    assert result.reason == "jev:confidence_insufficient"


def test_timeout_and_api_errors_fall_back_without_exposing_error_text() -> None:
    for error in (TimeoutError("token=secret"), RuntimeError("api_key=secret")):
        result = evaluate_approval(request(), StubProvider(error))
        assert result.decision is ApprovalDecision.FALLBACK_TO_USER
        assert result.reason in {"provider:timeout", "provider:error"}
        assert "secret" not in result.reason


def test_malformed_provider_assessment_falls_back() -> None:
    provider = StubProvider(assessment(safe_to_auto_approve=math.nan))

    result = evaluate_approval(request(), provider)

    assert result.decision is ApprovalDecision.FALLBACK_TO_USER
    assert result.reason == "provider:invalid_response"
