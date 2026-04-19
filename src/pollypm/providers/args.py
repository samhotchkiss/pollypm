"""Provider-argument sanitizers shared across launch entry points.

Contract:
- Inputs: provider CLI args plus the owning ``ProviderKind``.
- Outputs: a provider-compatible argument list safe to hand to launch
  planners, session managers, and supervisor entry points.
- Side effects: none.
- Invariants: flags belonging to a different provider are stripped, and
  an empty result falls back to the provider's default permissions flag.
- Allowed dependencies: ``pollypm.models.ProviderKind`` only.
- Private: the provider-specific flag tables.
"""

from __future__ import annotations

from pollypm.models import ProviderKind


_CODEX_ONLY_FLAGS = frozenset({
    "--dangerously-bypass-approvals-and-sandbox",
    "--sandbox",
    "--ask-for-approval",
})
_CLAUDE_ONLY_FLAGS = frozenset({
    "--dangerously-skip-permissions",
    "--allowedTools",
    "--disallowedTools",
})
_FLAGS_WITH_VALUES = frozenset({
    "--sandbox",
    "--ask-for-approval",
    "--allowedTools",
    "--disallowedTools",
})


def sanitize_provider_args(args: list[str], provider: ProviderKind) -> list[str]:
    """Remove flags that belong to a different provider."""
    bad_flags = _CODEX_ONLY_FLAGS if provider is ProviderKind.CLAUDE else _CLAUDE_ONLY_FLAGS
    cleaned: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in bad_flags:
            if arg in _FLAGS_WITH_VALUES:
                skip_next = True
            continue
        cleaned.append(arg)
    if not cleaned:
        if provider is ProviderKind.CLAUDE:
            return ["--dangerously-skip-permissions"]
        if provider is ProviderKind.CODEX:
            return ["--dangerously-bypass-approvals-and-sandbox"]
    return cleaned
