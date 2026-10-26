"""End-to-end tests for the FastAPI app: webhooks, scheduler, API and UI."""

from __future__ import annotations

import hashlib
import hmac
import json
import pathlib

import pytest

from openzap.scheduler import sync_scheduler
from openzap.triggers import run_scheduled_workflow


# ------------------------------------------------------------------------ webhook


def test_health_endpoint(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "openzap"}


def test_webhook_triggers_a_run_end_to_end(client, webhook_workflow):
    token = webhook_workflow["row"]["hook_token"]
    target: pathlib.Path = webhook_workflow["target"]

    response = client.post(
        f"/api/hooks/{token}",
        json={"email": "ada@example.com", "count": 2},
        headers={"X-Request-Id": "abc-123"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["workflow"] == "capture-webhook"
    assert body["status"] == "success"
    assert body["run_id"] > 0
    assert body["steps"] == [{"name": "write", "type": "write_file", "status": "success"}]

    assert target.exists(), "the write_file action must have created the file"
    assert json.loads(target.read_text()) == {"email": "ada@example.com", "count": 2}


def test_webhook_records_headers_and_query_in_the_trigger(client, webhook_workflow, tmp_path):
    token = webhook_workflow["row"]["hook_token"]
    target = tmp_path / "probe.json"
    client.post(
        f"/api/hooks/{token}?source=unit-test",
        json={"x": 1},
        headers={"X-Trace": "t-1"},
    )
    client.get("/api/workflows")
    # Inspect the recorded trigger payload through the run detail API.
    runs = client.get("/api/runs", params={"workflow": "capture-webhook"}).json()["runs"]
    trigger = runs[0]["input"]
    assert trigger["query"]["source"] == "unit-test"
    assert trigger["headers"]["x-trace"] == "t-1"
    assert trigger["body"] == {"x": 1}


def test_unknown_webhook_token_returns_404(client):
    response = client.post("/api/hooks/not-a-real-token", json={"a": 1})
    assert response.status_code == 404


def test_disabled_workflow_refuses_webhooks(client, store):
    row = store.save_workflow(
        {
            "name": "paused",
            "enabled": False,
            "trigger": {"type": "webhook", "config": {}},
            "steps": [{"name": "l", "type": "log", "message": "no"}],
        }
    )["row"]
    response = client.post(f"/api/hooks/{row['hook_token']}", json={"a": 1})
    assert response.status_code == 403


def test_webhook_body_size_limit_is_enforced(client, webhook_workflow):
    token = webhook_workflow["row"]["hook_token"]
    response = client.post(f"/api/hooks/{token}", json={"blob": "x" * 2000, "small": 1})
    assert response.status_code == 200  # small payload is fine
    big = client.post(f"/api/hooks/{token}", json={"blob": "x" * 2_000_000})
    assert big.status_code == 413


def test_webhook_signature_verification(client, store):
    row = store.save_workflow(
        {
            "name": "signed",
            "trigger": {"type": "webhook", "config": {"secret": "s3cret"}},
            "steps": [{"name": "l", "type": "log", "message": "signed"}],
        }
    )["row"]
    url = f"/api/hooks/{row['hook_token']}"
    body = json.dumps({"email": "a@example.com"}).encode()

    assert client.post(url, content=body, headers={"Content-Type": "application/json"}).status_code == 401
    assert (
        client.post(url, content=body, headers={"Content-Type": "application/json", "X-OpenZap-Signature": "sha256=deadbeef"}).status_code
        == 401
    )

    digest = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    good = client.post(
        url, content=body, headers={"Content-Type": "application/json", "X-OpenZap-Signature": digest}
    )
    assert good.status_code == 200
    assert good.json()["workflow"] == "signed"


# ------------------------------------------------------------------ scheduled


def test_schedule_trigger_runs_via_the_handler_directly(client, store):
    row = store.save_workflow(
        {
            "name": "tick",
            "trigger": {"type": "schedule", "config": {"cron": "*/5 * * * *", "payload": {"n": 1}}},
            "steps": [
                {
                    "name": "l",
                    "type": "log",
                    "file": str(store.settings.logs_dir / "tick.jsonl"),
                    "message": "tick {{ trigger.body.n }}",
                }
            ],
        }
    )["row"]

    report = run_scheduled_workflow(store, store.settings, "tick")
    assert report["status"] == "success"
    log_file = store.settings.logs_dir / "tick.jsonl"
    assert json.loads(log_file.read_text().strip())["message"] == "tick 1"

    runs = client.get("/api/runs", params={"workflow": "tick"}).json()["runs"]
    assert runs[0]["trigger_type"] == "schedule"
    assert runs[0]["status"] == "success"


def test_sync_scheduler_registers_only_enabled_cron_workflows(client, store, settings):
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler()
    store.save_workflow(
        {
            "name": "every-five",
            "trigger": {"type": "schedule", "config": {"cron": "*/5 * * * *"}},
            "steps": [{"name": "l", "type": "log", "message": "x"}],
        }
    )
    store.save_workflow(
        {
            "name": "paused-cron",
            "enabled": False,
            "trigger": {"type": "schedule", "config": {"cron": "*/5 * * * *"}},
            "steps": [{"name": "l", "type": "log", "message": "x"}],
        }
    )
    store.save_workflow(
        {
            "name": "on-webhook",
            "trigger": {"type": "webhook", "config": {}},
            "steps": [{"name": "l", "type": "log", "message": "x"}],
        }
    )

    scheduled = sync_scheduler(scheduler, store, settings)
    assert scheduled == ["every-five"]
    assert {job.id for job in scheduler.get_jobs()} == {"openzap-workflow:every-five"}

    # Disabling the workflow and re-syncing must remove its job.
    entry = store.get_workflow("every-five")["definition"]
    entry["enabled"] = False
    store.save_workflow(entry, keep_token=True)
    assert sync_scheduler(scheduler, store, settings) == []
    assert scheduler.get_jobs() == []


def test_reload_endpoint_resyncs_the_scheduler(client, store, settings):
    from apscheduler.schedulers.background import BackgroundScheduler

    app = client.app
    app.state.scheduler = BackgroundScheduler()
    app.state.scheduler.start()

    store.save_workflow(
        {
            "name": "late-job",
            "trigger": {"type": "schedule", "config": {"cron": "0 * * * *"}},
            "steps": [{"name": "l", "type": "log", "message": "x"}],
        }
    )
    response = client.post("/api/admin/reload")
    assert response.status_code == 200
    body = response.json()
    assert body["reloaded"] is True
    assert body["scheduled"] == ["late-job"]
    app.state.scheduler.shutdown(wait=False)


# ------------------------------------------------------------------------- API


def test_workflow_crud_through_the_api(client):
    definition = {
        "name": "api-created",
        "description": "created via the API",
        "enabled": True,
        "trigger": {"type": "webhook", "config": {}},
        "steps": [{"name": "l", "type": "log", "message": "hi"}],
    }
    created = client.post("/api/workflows", json=definition)
    assert created.status_code == 200
    assert created.json()["row"]["name"] == "api-created"

    listed = client.get("/api/workflows").json()["workflows"]
    assert any(w["name"] == "api-created" for w in listed)
    assert all(w["hook_url"].startswith("http") for w in listed if w["trigger_type"] == "webhook")

    fetched = client.get("/api/workflows/api-created")
    assert fetched.status_code == 200
    assert fetched.json()["definition"]["steps"][0]["type"] == "log"

    deleted = client.delete("/api/workflows/api-created")
    assert deleted.status_code == 200
    assert client.get("/api/workflows/api-created").status_code == 404


def test_invalid_workflow_definition_returns_400(client):
    bad = client.post(
        "/api/workflows",
        json={"name": "bad", "trigger": {"type": "nope", "config": {}}, "steps": []},
    )
    assert bad.status_code == 400
    assert "trigger" in bad.json()["detail"]


def test_manual_run_endpoint_records_a_run(client, webhook_workflow):
    response = client.post(
        "/api/workflows/capture-webhook/run",
        json={"manual": True, "n": 5},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["run_id"] > 0


# ------------------------------------------------------------------------ UI


def test_dashboard_renders_workflows_and_hook_urls(client, webhook_workflow):
    response = client.get("/")
    assert response.status_code == 200
    assert "capture-webhook" in response.text
    assert f"/api/hooks/{webhook_workflow['row']['hook_token']}" in response.text


def test_history_page_renders(client, webhook_workflow):
    client.post(f"/api/hooks/{webhook_workflow['row']['hook_token']}", json={"a": 1})
    response = client.get("/runs")
    assert response.status_code == 200
    assert "capture-webhook" in response.text


def test_workflow_view_and_editor_render(client, webhook_workflow):
    assert client.get("/workflows/capture-webhook").status_code == 200
    assert client.get("/workflows/capture-webhook/edit").status_code == 200
    assert client.get("/workflows/new").status_code == 200


def test_run_detail_page_renders(client, webhook_workflow):
    created = client.post(f"/api/hooks/{webhook_workflow['row']['hook_token']}", json={"a": 1})
    run_id = created.json()["run_id"]
    response = client.get(f"/runs/{run_id}")
    assert response.status_code == 200
    assert "capture-webhook" in response.text
