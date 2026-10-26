"""Tests for the execution engine: ordering, stop_on_error, error capture."""

from __future__ import annotations

import json
import pathlib

import pytest

from openzap.engine import RunReport, run_workflow
from openzap.errors import WorkflowError
from openzap.workflow import validate_workflow


def workflow(**overrides) -> dict:
    """A minimal two-step workflow writing into ``log_path``."""
    definition = {
        "name": "engine-test",
        "description": "",
        "enabled": True,
        "stop_on_error": True,
        "trigger": {"type": "webhook", "config": {}},
        "steps": [
            {"name": "first", "type": "log", "message": "first"},
            {"name": "second", "type": "log", "message": "second"},
        ],
    }
    definition.update(overrides)
    return validate_workflow(json.loads(json.dumps(definition)))


def payload():
    return {"type": "webhook", "body": {"email": "ada@example.com"}, "headers": {}, "query": {}}


def written_log(log_path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


def test_steps_run_in_defined_order(tmp_path: pathlib.Path):
    log_path = tmp_path / "order.jsonl"
    definition = workflow(
        steps=[
            {"name": "a", "type": "log", "file": str(log_path), "message": "a"},
            {"name": "b", "type": "log", "file": str(log_path), "message": "b"},
            {"name": "c", "type": "log", "file": str(log_path), "message": "c"},
        ]
    )
    report = run_workflow(definition, payload())
    assert report.status == "success"
    assert [entry["name"] for entry in report.steps] == ["a", "b", "c"]
    assert [record["message"] for record in written_log(log_path)] == ["a", "b", "c"]
    assert report.duration_ms >= 0


def test_step_results_are_visible_to_later_steps(tmp_path: pathlib.Path, http_server):
    base_url, _ = http_server
    log_path = tmp_path / "carry.jsonl"
    definition = workflow(
        steps=[
            {"name": "lookup", "type": "http_request", "url": f"{base_url}/json", "method": "GET"},
            {
                "name": "record",
                "type": "log",
                "file": str(log_path),
                "message": "got {{ steps.lookup.response.body.count }}",
            },
        ]
    )
    report = run_workflow(definition, payload())
    assert report.status == "success"
    assert written_log(log_path)[0]["message"] == "got 1"
    assert report.steps[0]["result"]["response"]["status_code"] == 200


def test_stop_on_error_halts_the_run(tmp_path: pathlib.Path):
    log_path = tmp_path / "halt.jsonl"
    definition = workflow(
        stop_on_error=True,
        steps=[
            {"name": "boom", "type": "log", "file": str(log_path), "message": "{{ trigger.body.nope }}"},
            {"name": "never", "type": "log", "file": str(log_path), "message": "never"},
        ],
    )
    report = run_workflow(definition, payload())
    assert report.status == "failed"
    assert report.error and "not found" in report.error
    assert [entry["name"] for entry in report.steps] == ["boom"]
    assert report.steps[0]["status"] == "failed"
    assert report.steps[0]["error"]


def test_stop_on_error_false_continues_with_remaining_steps(tmp_path: pathlib.Path):
    log_path = tmp_path / "continue.jsonl"
    definition = workflow(
        stop_on_error=False,
        steps=[
            {"name": "boom", "type": "log", "file": str(log_path), "message": "{{ trigger.body.nope }}"},
            {"name": "still-runs", "type": "log", "file": str(log_path), "message": "still"},
        ],
    )
    report = run_workflow(definition, payload())
    assert report.status == "failed"
    assert [entry["name"] for entry in report.steps] == ["boom", "still-runs"]
    assert report.steps[1]["status"] == "success"
    assert [r["message"] for r in written_log(log_path)] == ["still"]


def test_unknown_action_type_becomes_a_failed_step():
    # Deliberately skip validation: an unknown type only reaches the engine if
    # a workflow file is edited out from under the process.
    definition = {
        "name": "engine-test",
        "enabled": True,
        "stop_on_error": True,
        "trigger": {"type": "webhook", "config": {}},
        "steps": [{"name": "x", "type": "teleport", "url": "u"}],
    }
    report = run_workflow(definition, payload())
    assert report.status == "failed"
    assert "teleport" in (report.error or "")


def test_missing_step_template_path_is_captured_as_error():
    definition = workflow(steps=[{"name": "x", "type": "log", "message": "{{ trigger.body.missing }}"}])
    report = run_workflow(definition, payload())
    assert report.status == "failed"
    assert report.steps[0]["status"] == "failed"
    assert "missing" in report.steps[0]["error"]


def test_initial_steps_can_be_seeded_for_the_context():
    definition = workflow(steps=[{"name": "x", "type": "log", "message": "{{ steps.seed.value }}"}])
    report = run_workflow(definition, payload(), initial_steps={"seed": {"value": "ok"}})
    assert report.status == "success"


def test_report_to_dict_is_json_serialisable():
    report = RunReport(workflow="wf", trigger_type="webhook").to_dict()
    assert json.loads(json.dumps(report))["workflow"] == "wf"


def test_validation_rejects_bad_definitions():
    with pytest.raises(WorkflowError):
        validate_workflow({"name": "bad"})
    with pytest.raises(WorkflowError):
        validate_workflow({"name": "bad", "trigger": {"type": "cron", "config": {}}, "steps": []})
    with pytest.raises(WorkflowError):
        validate_workflow(
            {
                "name": "bad",
                "trigger": {"type": "schedule", "config": {"cron": "not a cron"}},
                "steps": [{"name": "a", "type": "log"}],
            }
        )
