"""``http_request`` action: make an HTTP call with templated headers and body.

Config
------
``method``      HTTP verb (default ``GET``, any casing)
``url``         Target URL (required)
``headers``     Mapping rendered through the template engine
``params``      Query string mapping, rendered the same way
``body``        String body (sent as-is) or mapping (sent as JSON)
``auth``        ``{"username": "...", "password": "..."}`` for basic auth
``timeout``     Seconds (default 30)
``follow_redirects``  Default ``true``
``verify_ssl``  Default ``true``
``success_status``    List of accepted status codes; defaults to 2xx/3xx

Result
------
``{"response": {"status_code", "headers", "body", "text", "url", "elapsed_ms"}}``
where ``body`` is the parsed JSON object when the response is JSON.
"""

from __future__ import annotations

import time
from typing import Any

import requests

from openzap.errors import ActionError


def _body_kwargs(body: Any) -> dict[str, Any]:
    """Choose ``data=`` for strings and ``json=`` for structured bodies."""
    if body is None:
        return {}
    if isinstance(body, (str, bytes)):
        return {"data": body}
    return {"json": body}


def run(config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Execute the request and return a normalised result dict."""
    url = config.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ActionError("'http_request' requires a 'url'")
    method = (config.get("method") or "GET").strip().upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        raise ActionError(f"Unsupported HTTP method {method!r}")

    try:
        timeout = float(config.get("timeout") or 30)
    except (TypeError, ValueError):
        raise ActionError("'timeout' must be a number of seconds") from None

    kwargs: dict[str, Any] = {
        "headers": config.get("headers") or {},
        "params": config.get("params") or {},
        "timeout": timeout,
        "allow_redirects": bool(config.get("follow_redirects", True)),
        "verify": bool(config.get("verify_ssl", True)),
        **_body_kwargs(config.get("body")),
    }
    auth = config.get("auth")
    if isinstance(auth, dict) and auth.get("username"):
        kwargs["auth"] = (auth["username"], auth.get("password", ""))

    started = time.monotonic()
    try:
        response = requests.request(method, url, **kwargs)
    except requests.RequestException as exc:
        raise ActionError(f"HTTP request to {url} failed: {exc}") from exc
    elapsed_ms = int((time.monotonic() - started) * 1000)

    text = response.text
    try:
        parsed = response.json()
    except ValueError:
        parsed = None

    result = {
        "response": {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": parsed,
            "text": text[:4000],
            "url": str(response.url),
            "elapsed_ms": elapsed_ms,
        }
    }

    expected = config.get("success_status")
    if expected is None:
        acceptable = 200 <= response.status_code < 400
    else:
        codes = expected if isinstance(expected, list) else [expected]
        acceptable = response.status_code in [int(c) for c in codes]

    if not acceptable:
        raise ActionError(
            f"HTTP {response.status_code} from {url}: {text[:300]}",
            result=result,
        )
    return result
