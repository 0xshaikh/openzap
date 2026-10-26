"""``slack_webhook`` action: post a message to a Slack incoming-webhook URL.

Config
------
``url``        Incoming webhook URL (required)
``text``       Shortcut text (required unless ``blocks`` is provided)
``channel``    Target channel override
``blocks``     Block-kit layout (list of block dicts)
``thread_ts``  Thread id for replies
``timeout``    Seconds (default 15)
``verify_ssl`` Default ``true``

Result
------
``{"response": {"status_code", "body", "elapsed_ms"}}``
"""

from __future__ import annotations

import time
from typing import Any

import requests

from openzap.errors import ActionError


def run(config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """POST the message payload to the configured Slack webhook."""
    url = config.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ActionError("'slack_webhook' requires a 'url'")
    if "hooks.slack.com" not in url and not config.get("allow_any_url"):
        raise ActionError(
            "Refusing to POST a Slack message to a non-Slack URL; set "
            "'allow_any_url': true to permit other endpoints"
        )

    payload: dict[str, Any] = {}
    if config.get("channel"):
        payload["channel"] = config["channel"]
    if config.get("text") is not None:
        payload["text"] = config["text"]
    if config.get("blocks") is not None:
        payload["blocks"] = config["blocks"]
    if config.get("thread_ts") is not None:
        payload["thread_ts"] = config["thread_ts"]
    if config.get("unfurl_links") is not None:
        payload["unfurl_links"] = bool(config["unfurl_links"])
    if not payload.get("text") and not payload.get("blocks"):
        raise ActionError("'slack_webhook' needs a 'text' or 'blocks' field")

    try:
        timeout = float(config.get("timeout") or 15)
    except (TypeError, ValueError):
        raise ActionError("'timeout' must be a number of seconds") from None

    started = time.monotonic()
    try:
        response = requests.post(
            url,
            json=payload,
            timeout=timeout,
            verify=bool(config.get("verify_ssl", True)),
        )
    except requests.RequestException as exc:
        raise ActionError(f"Slack webhook post to {url} failed: {exc}") from exc
    elapsed_ms = int((time.monotonic() - started) * 1000)

    result = {
        "response": {
            "status_code": response.status_code,
            "body": response.text[:1000],
            "elapsed_ms": elapsed_ms,
        }
    }
    if response.status_code != 200 or response.text.strip() != "ok":
        raise ActionError(
            f"Slack webhook rejected the message (HTTP {response.status_code}): "
            f"{response.text[:200]}",
            result=result,
        )
    return result
