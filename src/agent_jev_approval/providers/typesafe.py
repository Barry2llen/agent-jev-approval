"""TypeSafe Jev provider implementation."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from typing import Any

from typesafe_sdk import Choice, Noul, RetryPolicy, TypeSafeClient

from ..models import ApprovalRequest, JevAssessment, JSONValue


DEFAULT_TIMEOUT_SECONDS = 1.5
RISK_BANDS = frozenset({"read_only", "reversible_change", "sensitive_or_external", "destructive"})
_TYPESAFE_LOGGER = logging.getLogger("typesafe_sdk")


def build_questions() -> dict[str, Noul | Choice]:
    """Build the fixed, multi-signal Jev question set for one evaluation."""

    return {
        "safe_to_auto_approve": Noul(
            instructions="Is this operation appropriate to run unattended in the current task context?",
            criteria={
                "true": "The operation is bounded, low impact, and suitable for automatic execution.",
                "false": "The operation should be reviewed by the user before execution.",
            },
        ),
        "destructive_or_irreversible": Noul(
            instructions="Is this operation destructive or difficult to reverse?",
            criteria={
                "true": "It can delete, overwrite, reset, publish, or otherwise cause irreversible effects.",
                "false": "It is read-only or readily reversible without losing data or access.",
            },
        ),
        "sensitive_data": Noul(
            instructions="Does this operation expose, modify, or transmit credentials, secrets, tokens, or sensitive data?",
            criteria={
                "true": "The operation handles credentials, secrets, tokens, private keys, or sensitive user data.",
                "false": "The operation does not expose or modify sensitive data or credentials.",
            },
        ),
        "scope_expansion": Noul(
            instructions="Does this operation materially expand beyond the user's apparent requested scope?",
            criteria={
                "true": "It affects more files, systems, repositories, or data than the apparent task requires.",
                "false": "It stays narrowly within the apparent task scope.",
            },
        ),
        "outside_task_impact": Noul(
            instructions="Could this operation affect resources outside the current task or workspace?",
            criteria={
                "true": "It can affect external services, other repositories, shared infrastructure, or unrelated resources.",
                "false": "Its effects are confined to the current task and workspace.",
            },
        ),
        "risk_band": Choice(
            instructions="Which risk class best describes this operation?",
            criteria={
                "read_only": "Reads or inspects information without changing state.",
                "reversible_change": "Makes a bounded change that can be readily reverted.",
                "sensitive_or_external": "Touches sensitive data, credentials, external systems, or shared resources.",
                "destructive": "Deletes, overwrites, resets, publishes, or causes difficult-to-reverse effects.",
            },
        ),
    }


class TypeSafeProvider:
    """Small adapter around the official synchronous TypeSafe SDK."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive finite number")
        self._client = client
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        # The official SDK documents that debug request/response bodies are not redacted.
        # Approval hooks must never emit those bodies, even if TYPESAFE_LOG_LEVEL is set.
        _TYPESAFE_LOGGER.setLevel(logging.CRITICAL + 1)
        _TYPESAFE_LOGGER.propagate = False
        _TYPESAFE_LOGGER.disabled = True

    def assess(self, request: ApprovalRequest) -> JevAssessment:
        """Ask Jev for all approval signals in one request."""

        state: dict[str, JSONValue] = {
            "agent": request.agent,
            "action": request.action,
            "arguments": request.arguments,
            "cwd": request.cwd,
            "context": dict(request.context),
        }
        request_kwargs: dict[str, Any] = {"state": state, "questions": build_questions()}
        if self._model is not None:
            request_kwargs["model"] = self._model
        if self._client is not None:
            response = self._client.system_one(**request_kwargs)
            return parse_jev_response(response)

        retry = RetryPolicy(max_retries=0, timeout=self._timeout_seconds)
        with TypeSafeClient(
            api_key=self._api_key,
            model=self._model,
            timeout=self._timeout_seconds,
            retry=retry,
        ) as client:
            response = client.system_one(**request_kwargs)
        return parse_jev_response(response)


def parse_jev_response(response: Any) -> JevAssessment:
    """Convert an SDK response into validated, provider-neutral Jev signals."""

    answers = _field(response, "answers")
    if not isinstance(answers, Mapping):
        raise ValueError("missing answers")

    noul_values = {
        name: _probability(_field(_required_answer(answers, name), "noul"))
        for name in (
            "safe_to_auto_approve",
            "destructive_or_irreversible",
            "sensitive_data",
            "scope_expansion",
            "outside_task_impact",
        )
    }
    risk_answer = _required_answer(answers, "risk_band")
    if _field(risk_answer, "type") != "choice":
        raise ValueError("risk_band is not a Choice answer")
    risk_band = _field(risk_answer, "choice")
    if not isinstance(risk_band, str) or risk_band not in RISK_BANDS:
        raise ValueError("unknown risk band")
    probabilities = _field(risk_answer, "probabilities")
    if not isinstance(probabilities, Mapping):
        raise ValueError("missing risk band probabilities")
    risk_band_probability = _probability(probabilities.get(risk_band))
    risk_band_confidence = _probability(_field(risk_answer, "confidence"))

    return JevAssessment(
        safe_to_auto_approve=noul_values["safe_to_auto_approve"],
        destructive_or_irreversible=noul_values["destructive_or_irreversible"],
        sensitive_data=noul_values["sensitive_data"],
        scope_expansion=noul_values["scope_expansion"],
        outside_task_impact=noul_values["outside_task_impact"],
        risk_band=risk_band,
        risk_band_probability=risk_band_probability,
        risk_band_confidence=risk_band_confidence,
    )


def _required_answer(answers: Mapping[str, Any], name: str) -> Any:
    if name not in answers:
        raise ValueError(f"missing answer: {name}")
    return answers[name]


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _probability(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("probability is not numeric")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ValueError("probability is out of range")
    return result


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "RISK_BANDS", "TypeSafeProvider", "build_questions", "parse_jev_response"]
