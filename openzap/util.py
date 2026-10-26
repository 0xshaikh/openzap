"""Small shared helpers."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string with second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_hook_token() -> str:
    """Generate a high-entropy webhook token."""
    return secrets.token_urlsafe(24)
