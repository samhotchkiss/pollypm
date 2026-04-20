"""HTTP POST adapter for user-configured webhooks.

Points at any HTTP endpoint — ntfy.sh, Pushbullet, Slack webhook,
Discord, a home-rolled Home Assistant hook. Configured via
``[human_notify.webhook]`` in ``pollypm.toml``:

    [human_notify.webhook]
    url = "https://ntfy.sh/sam-pollypm"
    # optional:
    header_authorization = "Bearer …"   # extra Authorization header
    title_header = "Title"              # ntfy uses "Title"
    priority_header = "Priority"        # ntfy uses "Priority: 4"

The adapter sends the ``body`` string as the request payload
(``text/plain``) and pushes the ``title`` into whichever header
the service uses. JSON-shaped services can register their own
adapter via the ``pollypm.human_notifier`` entry-point group — this
one is deliberately the plaintext-friendly baseline that covers
the long tail of webhook services.

Kept stdlib-only via :mod:`urllib.request` so PollyPM's runtime
dependency surface doesn't grow for this feature.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 3.0


class WebhookNotifyAdapter:
    """``HumanNotifyAdapter`` — HTTP POST to a user-configured URL.

    Config lives on the adapter instance; the plugin constructor
    reads ``[human_notify.webhook]`` from ``pollypm.toml`` and
    passes resolved values here.
    """

    name = "webhook"

    def __init__(
        self,
        *,
        url: str | None,
        authorization: str | None = None,
        title_header: str = "Title",
        priority_header: str | None = "Priority",
        default_priority: str | None = "4",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.url = url
        self.authorization = authorization
        self.title_header = title_header
        self.priority_header = priority_header
        self.default_priority = default_priority
        self.timeout_seconds = timeout_seconds
        self.extra_headers = dict(extra_headers or {})

    def is_available(self) -> bool:
        """True iff the user has configured a webhook URL.

        No URL = opt-out; the adapter silently skips so a config-less
        install doesn't get webhook warnings in its logs.
        """
        return bool(self.url)

    def notify(
        self,
        *,
        title: str,
        body: str,
        task_id: str,
        project: str,
    ) -> None:
        """POST ``body`` to the configured URL with ``title`` in the header.

        ``task_id`` / ``project`` are also included as structured
        headers (``X-PollyPM-Task``, ``X-PollyPM-Project``) so
        downstream services can route / filter without parsing the
        body. Failure is logged and swallowed — the cockpit fallback
        still delivers, and the dispatcher continues to other
        adapters.
        """
        if self.url is None:
            return  # is_available should have caught this; defensive
        headers: dict[str, str] = {
            "Content-Type": "text/plain; charset=utf-8",
            "X-PollyPM-Task": task_id,
            "X-PollyPM-Project": project,
        }
        if self.title_header:
            headers[self.title_header] = title
        if self.priority_header and self.default_priority:
            headers[self.priority_header] = self.default_priority
        if self.authorization:
            headers["Authorization"] = self.authorization
        headers.update(self.extra_headers)

        req = urllib.request.Request(
            self.url,
            data=body.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                if resp.status >= 300:
                    logger.warning(
                        "human_notify[webhook]: POST %s returned %d",
                        self.url, resp.status,
                    )
        except urllib.error.URLError as exc:
            logger.warning(
                "human_notify[webhook]: POST %s failed: %s", self.url, exc,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("human_notify[webhook]: unexpected error: %s", exc)

    def as_dict(self) -> dict[str, object]:
        """Debug / test helper — ``json.dumps``-safe snapshot of config."""
        return {
            "url": self.url,
            "authorization_set": bool(self.authorization),
            "title_header": self.title_header,
            "priority_header": self.priority_header,
            "default_priority": self.default_priority,
            "timeout_seconds": self.timeout_seconds,
            "extra_headers": dict(self.extra_headers),
        }


def from_config(raw_config: dict) -> "WebhookNotifyAdapter":
    """Build an adapter from a parsed ``[human_notify.webhook]`` block.

    Tolerant of missing keys — an adapter built from ``{}`` returns
    ``False`` from :meth:`is_available` and is silently skipped.
    """
    if not isinstance(raw_config, dict):
        return WebhookNotifyAdapter(url=None)
    url = raw_config.get("url")
    authorization = raw_config.get("header_authorization") or raw_config.get("authorization")
    title_header = raw_config.get("title_header", "Title")
    priority_header = raw_config.get("priority_header", "Priority")
    default_priority = raw_config.get("default_priority", "4")
    timeout = raw_config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    extra = raw_config.get("extra_headers") or {}
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS
    return WebhookNotifyAdapter(
        url=url if isinstance(url, str) else None,
        authorization=authorization if isinstance(authorization, str) else None,
        title_header=title_header if isinstance(title_header, str) else "Title",
        priority_header=priority_header if isinstance(priority_header, str) else None,
        default_priority=default_priority if isinstance(default_priority, str) else None,
        timeout_seconds=timeout,
        extra_headers=extra if isinstance(extra, dict) else {},
    )


# Re-export ``json`` so callers that want to wrap the adapter in a
# JSON-payload builder (e.g. Slack webhooks) can ``from .webhook
# import json`` without pulling their own import — kept because the
# adapter's own body is plaintext.
__all__ = ["WebhookNotifyAdapter", "from_config"]

_ = json  # silence unused-import; reserved for subclass JSON wrappers
