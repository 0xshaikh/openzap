"""Shared pytest fixtures for the OpenZap test suite."""

from __future__ import annotations

import json
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from openzap.config import Settings
from openzap.storage import Store


@pytest.fixture
def home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Point OPENZAP_HOME at a fresh temp directory and clear SMTP config."""
    directory = tmp_path / "data"
    monkeypatch.setenv("OPENZAP_HOME", str(directory))
    monkeypatch.setenv("OPENZAP_BASE_URL", "")
    monkeypatch.setenv("NO_COLOR", "1")  # keep CLI output free of ANSI escapes
    for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM"):
        monkeypatch.delenv(key, raising=False)
    return directory


@pytest.fixture
def settings(home: pathlib.Path) -> Settings:
    """Environment-derived settings for the test data directory."""
    return Settings()


@pytest.fixture
def store(settings: Settings) -> Iterator[Store]:
    """A SQLite + filesystem store backed by the temp data directory."""
    created = Store(settings)
    yield created
    created.close()


@pytest.fixture
def app(settings: Settings, store: Store):
    """A FastAPI application wired to the temp store."""
    from openzap.server import create_app

    return create_app(settings, store)


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    """A synchronous FastAPI test client (no server socket needed)."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def webhook_workflow(store: Store, settings: Settings, tmp_path: pathlib.Path) -> dict:
    """A webhook-triggered workflow that writes the payload into a file."""
    target = tmp_path / "captured.json"
    definition = {
        "name": "capture-webhook",
        "description": "Write the incoming webhook payload to disk",
        "enabled": True,
        "stop_on_error": True,
        "trigger": {"type": "webhook", "config": {}},
        "steps": [
            {
                "name": "write",
                "type": "write_file",
                "path": str(target),
                "data": "{{ trigger.body }}",
            }
        ],
    }
    saved = store.save_workflow(definition)
    return {"definition": definition, "target": target, "row": saved["row"]}


class _RecordingHandler(BaseHTTPRequestHandler):
    """Tiny HTTP server used to exercise the http_request / slack actions."""

    server_version = "OpenZapTest/1.0"
    seen: list[dict] = []

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        self._respond(self.command)

    def do_POST(self):  # noqa: N802
        self._respond(self.command)

    def do_PUT(self):  # noqa: N802
        self._respond(self.command)

    def _respond(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        self.__class__.seen.append(
            {
                "method": method,
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": raw,
            }
        )
        if self.path.startswith("/json"):
            payload = b'{"ok": true, "count": 1}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif self.path.startswith("/echo"):
            payload = json.dumps(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers.items()),
                    "body": raw.decode("utf-8", "replace"),
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif self.path.startswith("/slack"):
            payload = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif self.path.startswith("/boom"):
            self.send_response(503)
            payload = b"service unavailable"
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args) -> None:  # silence test request logs
        return None


@pytest.fixture
def http_server() -> Iterator[tuple[str, list[dict]]]:
    """A real local HTTP server on an ephemeral port.

    Yields ``(base_url, seen)`` where ``seen`` is a list of recorded requests.
    """
    _RecordingHandler.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base_url, _RecordingHandler.seen
    finally:
        server.shutdown()
        server.server_close()
