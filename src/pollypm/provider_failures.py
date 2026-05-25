"""Provider-facing failure text classifiers.

The heartbeat backend and supervisor alert boundary both inspect live
provider panes. Keep the provider error vocabulary here so quota/login
wording does not drift between those callers.
"""

from __future__ import annotations


AUTH_FAILURE_PATTERNS: tuple[str, ...] = (
    "authentication failure",
    "invalid authentication credentials",
    "authentication_error",
    "not authenticated",
    "not logged in",
    "login required",
    "please run /login",
    "run /login",
    "please login",
    "please log in",
    "invalid api key",
    "disabled claude subscription",
    "use an anthropic api key",
)

CAPACITY_FAILURE_PATTERNS: tuple[str, ...] = (
    "usage limit",
    "quota exceeded",
    "0% left",
    "out of credits",
    "credit balance is too low",
    "you've hit your limit",
    "you have hit your limit",
    "hit your limit",
    "usage-credits",
)

PROVIDER_OUTAGE_PATTERNS: tuple[str, ...] = (
    "temporarily unavailable",
    "try again later",
    "server error",
    "overloaded",
    "service unavailable",
)


def _contains_any(text: str, patterns: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(pattern in lowered for pattern in patterns)


def has_auth_failure(text: str) -> bool:
    return _contains_any(text, AUTH_FAILURE_PATTERNS)


def has_capacity_failure(text: str) -> bool:
    return _contains_any(text, CAPACITY_FAILURE_PATTERNS)


def has_provider_outage(text: str) -> bool:
    return _contains_any(text, PROVIDER_OUTAGE_PATTERNS)


__all__ = [
    "AUTH_FAILURE_PATTERNS",
    "CAPACITY_FAILURE_PATTERNS",
    "PROVIDER_OUTAGE_PATTERNS",
    "has_auth_failure",
    "has_capacity_failure",
    "has_provider_outage",
]
