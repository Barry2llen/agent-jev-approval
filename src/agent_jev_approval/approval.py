"""Deep approval engine shared by all future agent adapters."""

from __future__ import annotations

import math
from typing import Protocol

from .models import ApprovalDecision, ApprovalRequest, ApprovalResult, JevAssessment
from .policy import DEFAULT_POLICY, RISK_SIGNAL_NAMES, SAFE_RISK_BANDS, PolicyConfig, hard_rule_match, validate_assessment


class ApprovalProvider(Protocol):
    """The small seam an external decision provider must satisfy."""

    def assess(self, request: ApprovalRequest) -> JevAssessment:
        """Return typed risk signals for one normalized request."""


def evaluate_approval(
    request: ApprovalRequest,
    provider: ApprovalProvider,
    *,
    policy: PolicyConfig = DEFAULT_POLICY,
) -> ApprovalResult:
    """Evaluate one request and fail closed to the user's native approval flow.

    The function never raises provider or policy exceptions. Its caller receives a
    stable result with a non-sensitive reason code, which makes it safe to use from
    a synchronous command hook.
    """

    try:
        hard_rule = hard_rule_match(request)
        if hard_rule is not None:
            return ApprovalResult(
                decision=ApprovalDecision.FALLBACK_TO_USER,
                reason=f"hard_rule:{hard_rule.code}",
            )

        assessment = provider.assess(request)
        validate_assessment(assessment)
        return _decide_from_assessment(assessment, policy)
    except Exception as error:  # noqa: BLE001 - fail-to-user is the safety invariant.
        return ApprovalResult(
            decision=ApprovalDecision.FALLBACK_TO_USER,
            reason=f"provider:{_safe_error_code(error)}",
        )


def _decide_from_assessment(assessment: JevAssessment, policy: PolicyConfig) -> ApprovalResult:
    risk_values = {name: float(getattr(assessment, name)) for name in RISK_SIGNAL_NAMES}
    aggregate_confidence = min(
        assessment.safe_to_auto_approve,
        *(1.0 - value for value in risk_values.values()),
        assessment.risk_band_probability,
        assessment.risk_band_confidence,
    )

    if assessment.safe_to_auto_approve < policy.min_safe_probability:
        return _fallback("jev:unsafe_to_auto_approve", aggregate_confidence)

    for name, probability in risk_values.items():
        if probability > policy.max_risk_probability:
            return _fallback(f"jev:risk_{name}", aggregate_confidence)

    if assessment.risk_band not in SAFE_RISK_BANDS:
        return _fallback("jev:unsafe_risk_band", aggregate_confidence)
    if assessment.risk_band_probability < policy.min_safe_band_probability:
        return _fallback("jev:risk_band_probability", aggregate_confidence)
    if assessment.risk_band_confidence < policy.min_confidence:
        return _fallback("jev:confidence_insufficient", aggregate_confidence)

    return ApprovalResult(
        decision=ApprovalDecision.ALLOW,
        reason="jev:thresholds_satisfied",
        confidence=aggregate_confidence,
    )


def _fallback(reason: str, confidence: float | None) -> ApprovalResult:
    return ApprovalResult(
        decision=ApprovalDecision.FALLBACK_TO_USER,
        reason=reason,
        confidence=confidence,
    )


def _safe_error_code(error: BaseException) -> str:
    name = type(error).__name__.lower()

    # Keep Provider diagnostics useful without ever exposing exception text. The
    # official TypeSafe SDK has stable exception class names and HTTP status
    # attributes, so prefer those over matching arbitrary messages.
    if "timeout" in name:
        return "timeout"
    if "responsevalidation" in name or "validation" in name:
        return "invalid_response"

    named_codes = (
        ("authentication", "authentication"),
        ("unauthorized", "authentication"),
        ("permissiondenied", "permission_denied"),
        ("forbidden", "permission_denied"),
        ("ratelimit", "rate_limited"),
        ("badrequest", "bad_request"),
        ("unprocessableentity", "unprocessable_entity"),
        ("notfound", "not_found"),
        ("internalserver", "server_error"),
    )
    for marker, code in named_codes:
        if marker in name:
            return code

    try:
        status = getattr(error, "status", None)
    except Exception:  # noqa: BLE001 - error classification must never escape.
        status = None
    if isinstance(status, int) and not isinstance(status, bool):
        status_codes = {
            400: "bad_request",
            401: "authentication",
            403: "permission_denied",
            404: "not_found",
            422: "unprocessable_entity",
            429: "rate_limited",
        }
        if status in status_codes:
            return status_codes[status]
        if status >= 500:
            return "server_error"

    if "connection" in name:
        return "connection"
    if name == "typesafeerror":
        # Config.resolve() uses this stable prefix for an absent key. We only
        # inspect it to select a code; the message is never returned or logged.
        try:
            message = str(error).strip().lower()
        except Exception:  # noqa: BLE001 - preserve fail-to-user on broken errors.
            message = ""
        if message.startswith("no api key was provided"):
            return "missing_api_key"
        return "sdk_error"
    if "apierror" in name or "http" in name:
        return "api_error"
    if isinstance(error, (ValueError, TypeError, KeyError, AttributeError)):
        return "invalid_response"
    return "error"


__all__ = ["ApprovalProvider", "evaluate_approval"]
