"""``pollypm.providers.codex`` — Codex provider package (Phase C of #397).

This package replaces the old ``providers/codex.py`` single-file module
and the Phase A ``LegacyCodexAdapter`` placeholder. It holds:

* :class:`CodexProvider` — satisfies the
  :class:`pollypm.acct.protocol.ProviderAdapter` Protocol; registered
  as the ``codex`` entry point in ``pyproject.toml``.
* :class:`CodexAdapter` — the launch-command adapter consumed by the
  tmux launch path and the ``core_codex`` built-in plugin. Previously
  at ``pollypm.providers.codex.CodexAdapter`` (via the single-file
  module) — the import path is preserved by re-exporting here.
* Sub-modules :mod:`.detect`, :mod:`.login`, :mod:`.probe`,
  :mod:`.env`, :mod:`.usage_parse` — the helpers :class:`CodexProvider`
  delegates to. Back-compat shims in ``pollypm.onboarding`` and
  ``pollypm.accounts`` forward to these symbols so existing call sites
  keep working.

The dual export (``CodexProvider`` *and* ``CodexAdapter``) is
deliberate: the two classes implement different Protocols at different
layers of the stack. Phase D will collapse them once the manager API
can carry both surfaces.
"""

from __future__ import annotations

from .adapter import CodexAdapter
from .detect import detect_codex_email, detect_email_from_pane, detect_logged_in
from .env import codex_profile_dir, isolated_env
from .login import run_login_flow
from .probe import probe_usage
from .provider import CodexProvider
from .usage_parse import parse_codex_status_text

__all__ = [
    "CodexAdapter",
    "CodexProvider",
    "codex_profile_dir",
    "detect_codex_email",
    "detect_email_from_pane",
    "detect_logged_in",
    "isolated_env",
    "parse_codex_status_text",
    "probe_usage",
    "run_login_flow",
]
