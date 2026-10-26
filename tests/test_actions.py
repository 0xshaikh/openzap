"""Tests for individual actions against a real local HTTP server."""

from __future__ import annotations

import json
import pathlib

import pytest

from openzap.actions.email_action import run as email_run
from openzap.actions.http_request import run as http_run
from openzap.actions.log_action import run as log_run
from openzap.actions.slack_webhook import run as slack_run
from openzap.actions.write_file import run as write_file_run
from openzap.config import Settings
from openzap.errors import ActionError


# ---------------------------------------------------------------- http_request


def test_http_request_returns_parsed_body_and_status(http_server):
    base_url, seen = http_server
    result = http_run({"url": f"{base_url}/json", "method": "GET"}, {})
    response = result["response"]
    assert response["status_code"] == 200
    assert response["body"] == {"ok": True, "count": 1}
    assert response["elapsed_ms"] >= 0
    assert seen[0]["path"].startswith("/json")


def test_http_request_forwards_templated_headers_and_json_body(http_server):
    base_url, seen = http_server
    result = http_run(
        {
            "url": f"{base_url}/echo",
            "method": "POST",
            "headers": {"X-Trace": "abc"},
            "body": {"email": "ada@example.com"},
        },
        {},
    )
    assert result["response"]["status_code"] == 200
    assert seen[0]["headers"]["X-Trace"] == "abc"
    assert json.loads(seen[0]["body"]) == {"email": "ada@example.com"}


def test_http_request_raises_action_error_on_bad_status(http_server):
    base_url, _ = http_server
    with pytest.raises(ActionError) as excinfo:
        http_run({"url": f"{base_url}/boom", "method": "GET"}, {})
    assert "503" in str(excinfo.value)
    # The partial result is preserved so the run log still shows the status.
    assert excinfo.value.result["response"]["status_code"] == 503


def test_http_request_success_status_override(http_server):
    base_url, _ = http_server
    result = http_run({"url": f"{base_url}/boom", "success_status": [503]}, {})
    assert result["response"]["status_code"] == 503


def test_http_request_requires_a_url():
    with pytest.raises(ActionError, match="url"):
        http_run({"method": "GET"}, {})


def test_http_request_connection_failure_is_reported():
    with pytest.raises(ActionError, match="failed"):
        http_run({"url": "http://127.0.0.1:1/nope", "timeout": 0.5}, {})


# ------------------------------------------------------------------- write_file


def test_write_file_writes_rendered_json(tmp_path: pathlib.Path):
    target = tmp_path / "out" / "payload.json"
    result = write_file_run(
        {"path": str(target), "data": {"a": 1, "b": [2, 3]}},
        {"_settings": None},
    )
    assert result["bytes_written"] > 0
    assert json.loads(target.read_text()) == {"a": 1, "b": [2, 3]}


def test_write_file_appends_jsonl_lines(tmp_path: pathlib.Path):
    target = tmp_path / "leads.jsonl"
    write_file_run({"path": str(target), "data": {"id": 1}, "append": True}, {"_settings": None})
    write_file_run({"path": str(target), "data": {"id": 2}, "append": True}, {"_settings": None})
    lines = target.read_text().strip().splitlines()
    assert [json.loads(line)["id"] for line in lines] == [1, 2]


def test_write_file_defaults_to_the_trigger_body(tmp_path: pathlib.Path):
    target = tmp_path / "default.json"
    write_file_run({"path": str(target)}, {"trigger": {"body": {"x": "y"}}})
    assert json.loads(target.read_text()) == {"x": "y"}


def test_write_file_sandboxes_relative_paths_inside_the_data_dir(tmp_path: pathlib.Path):
    settings = Settings(home=tmp_path / "home")
    result = write_file_run({"path": "sub/rel.json", "data": {"ok": 1}}, {"_settings": settings})
    assert result["path"].startswith(str(tmp_path / "home"))
    assert (tmp_path / "home" / "sub" / "rel.json").exists()


def test_write_file_requires_a_path():
    with pytest.raises(ActionError, match="path"):
        write_file_run({}, {})


# --------------------------------------------------------------------------- log


def test_log_appends_valid_jsonl(tmp_path: pathlib.Path):
    log_file = tmp_path / "runs.jsonl"
    context = {"workflow": {"name": "wf", "description": ""}, "_step_name": "step1"}
    result = log_run({"file": str(log_file), "message": "hello", "data": {"n": 1}}, context)
    assert result["file"] == str(log_file)
    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["workflow"] == "wf"
    assert record["step"] == "step1"
    assert record["message"] == "hello"
    assert record["data"] == {"n": 1}
    assert record["timestamp"]


# ------------------------------------------------------------------ slack_webhook


def test_slack_webhook_posts_a_message_payload(http_server):
    base_url, seen = http_server
    result = slack_run(
        {
            "url": f"{base_url}/slack",
            "allow_any_url": True,
            "text": "hello there",
        },
        {},
    )
    assert result["response"]["status_code"] == 200
    assert json.loads(seen[0]["body"]) == {"text": "hello there"}


def test_slack_webhook_refuses_non_slack_urls():
    with pytest.raises(ActionError, match="non-Slack"):
        slack_run({"url": "http://example.com/hook", "text": "hi"}, {})


def test_slack_webhook_requires_text_or_blocks():
    with pytest.raises(ActionError, match="text"):
        slack_run({"url": "https://hooks.slack.com/services/a/b/c", "channel": "#x"}, {})


# --------------------------------------------------------------------------- email


def test_email_skips_cleanly_without_smtp_config(settings: Settings):
    result = email_run({"to": "a@example.com", "subject": "s", "body": "b"}, {"_settings": settings})
    assert result["skipped"] is True
    assert "SMTP" in result["reason"]
    assert result.get("sent") is None


def test_email_reports_a_delivery_failure(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_PORT", "1")
    monkeypatch.setenv("SMTP_USER", "")
    settings_home = settings
    from openzap.actions.email_action import run

    with pytest.raises(ActionError, match="SMTP delivery"):
        run({"to": "a@example.com", "subject": "s", "body": "b"}, {"_settings": settings_home})
