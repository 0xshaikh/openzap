"""Trigger envelope builders and shared run helpers.

Both trigger types hand the engine the same shape, so actions do not care how
a run was started::

    {
      "type": "webhook" | "schedule",
      "body": {...},            # parsed request body, or the cron payload
      "headers": {...},         # webhook only
      "query": {...},           # webhook only
      "raw": "....",            # webhook only
      "received_at": "ISO-8601"
    }
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from openzap.config import Settings
from openzap.engine import RunReport, run_workflow
from openzap.storage import Store
from openzap.util import utcnow_iso

LOGGER = logging.getLogger("openzap.triggers")


def build_webhook_payload(
    *,
    body_bytes: bytes,
    headers: dict[str, str],
    query: dict[str, str],
) -> dict[str, Any]:
    """Normalise an inbound HTTP request into a trigger envelope."""
    raw = body_bytes.decode("utf-8", errors="replace")
    body: Any
    if not raw:
        body = {}
    else:
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"text": raw}
    return {
        "type": "webhook",
        "body": body,
        "headers": dict(headers),
        "query": dict(query),
        "raw": raw[:20000],
        "received_at": utcnow_iso(),
    }


def verify_webhook_signature(
    secret: str,
    raw_body: bytes,
    signature_header: str | None,
) -> bool:
    """Check an ``X-OpenZap-Signature: sha256=<hex>`` HMAC header."""
    if not secret:
        return True  # No secret configured: no signature expected.
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.strip())


def build_schedule_payload(workflow_definition: dict[str, Any]) -> dict[str, Any]:
    """Build the envelope for a cron-triggered run."""
    config = (workflow_definition.get("trigger") or {}).get("config") or {}
    return {
        "type": "schedule",
        "body": config.get("payload", {}),
        "headers": {},
        "query": {},
        "raw": "",
        "cron": config.get("cron", ""),
        "received_at": utcnow_iso(),
    }


def build_manual_payload(body: Any, source: str = "cli") -> dict[str, Any]:
    """Wrap an ad-hoc JSON input into the same envelope triggers use.

    Manual runs always expose the input as ``trigger.body`` so workflows
    written for webhooks can be exercised from the CLI or the UI unchanged.
    """
    return {
        "type": "manual",
        "body": body,
        "headers": {},
        "query": {},
        "raw": "",
        "source": source,
        "received_at": utcnow_iso(),
    }


def run_workflow_by_name(
    store: Store,
    settings: Settings,
    name: str,
    trigger_payload: dict[str, Any],
) -> dict[str, Any]:
    """Load a workflow, execute it and record the run. Returns the report dict."""
    entry = store.get_workflow(name)
    if entry is None:
        raise KeyError(f"Unknown workflow {name!r}")
    report: RunReport = run_workflow(
        entry["definition"], trigger_payload, settings=settings
    )
    run_id = store.record_run(report.to_dict(), trigger_payload)
    LOGGER.info(
        "workflow=%s status=%s duration_ms=%d run_id=%d",
        name,
        report.status,
        report.duration_ms,
        run_id,
    )
    result = report.to_dict()
    result["run_id"] = run_id
    return result


def run_scheduled_workflow(store: Store, settings: Settings, name: str) -> dict[str, Any]:
    """Entry point invoked by APScheduler for cron-triggered workflows."""
    entry = store.get_workflow(name)
    if entry is None:
        LOGGER.warning("Scheduled workflow %r is no longer defined; skipping", name)
        return {}
    payload = build_schedule_payload(entry["definition"])
    return run_workflow_by_name(store, settings, name, payload)
