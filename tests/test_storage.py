"""Tests for the persistence layer and workflow validation."""

from __future__ import annotations

import json
import pathlib

import pytest

from openzap.errors import WorkflowError
from openzap.storage import Store
from openzap.workflow import parse_workflow_file, validate_workflow, workflow_slug

EXAMPLES = pathlib.Path(__file__).parent.parent / "examples"


def minimal(**overrides):
    definition = {
        "name": "demo",
        "description": "demo workflow",
        "enabled": True,
        "trigger": {"type": "webhook", "config": {}},
        "steps": [{"name": "l", "type": "log", "message": "hello"}],
    }
    definition.update(overrides)
    return definition


def test_save_then_load_roundtrips_the_definition(store: Store, settings):
    saved = store.save_workflow(minimal())
    assert saved["row"]["name"] == "demo"
    assert saved["row"]["hook_token"]
    assert pathlib.Path(saved["row"]["file_path"]).exists()

    loaded = store.get_workflow("demo")
    assert loaded["definition"]["name"] == "demo"
    assert loaded["definition"]["steps"][0]["message"] == "hello"


def test_workflow_files_are_stored_inside_the_data_directory(store: Store, settings):
    row = store.save_workflow(minimal())["row"]
    assert str(settings.workflows_dir) in row["file_path"]
    assert row["file_path"].endswith("demo.json")


def test_list_workflows_is_sorted_and_filters_deleted(store: Store):
    store.save_workflow(minimal(name="zz"))
    store.save_workflow(minimal(name="aa"))
    names = [row["name"] for row in store.list_workflows()]
    assert names == ["aa", "zz"]
    assert store.delete_workflow("aa") is True
    assert [row["name"] for row in store.list_workflows()] == ["zz"]
    assert store.delete_workflow("aa") is False


def test_resaving_keeps_the_webhook_token_when_asked(store: Store):
    first = store.save_workflow(minimal())["row"]["hook_token"]
    store.save_workflow(minimal(description="updated"), keep_token=True)
    assert store.get_workflow("demo")["row"]["hook_token"] == first

    store.save_workflow(minimal(description="rotated"))
    assert store.get_workflow("demo")["row"]["hook_token"] != first


def test_deleting_removes_the_json_file(store: Store):
    row = store.save_workflow(minimal())["row"]
    path = pathlib.Path(row["file_path"])
    assert path.exists()
    store.delete_workflow("demo")
    assert not path.exists()


def test_webhook_url_is_only_returned_for_webhooks(store: Store, settings):
    webhook = store.save_workflow(minimal())["row"]
    scheduled = store.save_workflow(
        minimal(name="cron", trigger={"type": "schedule", "config": {"cron": "*/5 * * * *"}})
    )["row"]
    assert store.webhook_url("demo").endswith(webhook["hook_token"])
    assert store.webhook_url("cron") is None
    assert store.webhook_url("missing") is None


def test_find_workflow_by_token_resolves(store: Store):
    row = store.save_workflow(minimal())["row"]
    entry = store.find_workflow_by_token(row["hook_token"])
    assert entry["row"]["name"] == "demo"
    assert store.find_workflow_by_token("nonsense") is None


def test_run_history_is_recorded_and_filtered(store: Store):
    report = {
        "workflow": "demo",
        "trigger_type": "webhook",
        "status": "success",
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:01+00:00",
        "duration_ms": 12,
        "error": None,
        "steps": [{"name": "l", "type": "log", "status": "success", "result": {}}],
    }
    run_id = store.record_run(report, {"body": {"n": 1}})
    assert run_id > 0

    other = dict(report)
    other["workflow"] = "other"
    store.record_run(other, {})

    all_runs = store.list_runs()
    assert len(all_runs) == 2
    assert {run["workflow_name"] for run in all_runs} == {"demo", "other"}
    # Runs are ordered newest-first, so the record we just wrote is first.
    newest = all_runs[0]
    assert newest["workflow_name"] == "other"
    demo_run = store.list_runs(workflow_name="demo")[0]
    assert demo_run["input"] == {"body": {"n": 1}}
    assert demo_run["steps"][0]["name"] == "l"

    demo_only = store.list_runs(workflow_name="demo")
    assert len(demo_only) == 1
    assert store.get_run(run_id)["status"] == "success"
    assert store.get_run(999999) is None
    assert store.last_run("demo") is not None
    assert store.last_run("nope") is None


def test_validate_rejects_invalid_names_and_actions():
    for definition in (
        {"name": "", "trigger": {"type": "webhook", "config": {}}, "steps": [{"name": "a", "type": "log"}]},
        {"name": "bad name!", "trigger": {"type": "webhook", "config": {}}, "steps": [{"name": "a", "type": "log"}]},
        {
            "name": "ok",
            "trigger": {"type": "webhook", "config": {}},
            "steps": [{"name": "a", "type": "not-an-action"}],
        },
        {
            "name": "ok",
            "trigger": {"type": "webhook", "config": {}},
            "steps": [{"name": "dup", "type": "log"}, {"name": "dup", "type": "log"}],
        },
    ):
        with pytest.raises(WorkflowError):
            validate_workflow(definition)


def test_validate_requires_a_to_field_for_email_steps():
    with pytest.raises(WorkflowError, match="'to'"):
        validate_workflow(
            {
                "name": "ok",
                "trigger": {"type": "webhook", "config": {}},
                "steps": [{"name": "mail", "type": "email", "subject": "s"}],
            }
        )


def test_validate_defaults_are_applied():
    definition = validate_workflow(
        {"name": "ok", "trigger": {"type": "webhook"}, "steps": [{"type": "log"}]}
    )
    assert definition["enabled"] is True
    assert definition["stop_on_error"] is True
    assert definition["steps"][0]["name"] == "step_1"
    assert definition["schema_version"] == 1


def test_schedule_trigger_requires_a_valid_cron():
    with pytest.raises(WorkflowError, match="cron"):
        validate_workflow({"name": "ok", "trigger": {"type": "schedule", "config": {}}, "steps": [{"type": "log"}]})
    with pytest.raises(WorkflowError, match="5 fields"):
        validate_workflow(
            {"name": "ok", "trigger": {"type": "schedule", "config": {"cron": "* * *"}}, "steps": [{"type": "log"}]}
        )
    with pytest.raises(WorkflowError, match="Invalid cron"):
        validate_workflow(
            {
                "name": "ok",
                "trigger": {"type": "schedule", "config": {"cron": "99 99 99 99 99"}},
                "steps": [{"type": "log"}],
            }
        )


def test_parse_workflow_file_reads_json_and_rejects_bad_json(tmp_path: pathlib.Path):
    good = tmp_path / "ok.json"
    good.write_text(json.dumps(minimal()), encoding="utf-8")
    assert parse_workflow_file(good)["name"] == "demo"

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(WorkflowError, match="invalid JSON"):
        parse_workflow_file(bad)

    not_object = tmp_path / "list.json"
    not_object.write_text("[]", encoding="utf-8")
    with pytest.raises(WorkflowError, match="JSON object"):
        parse_workflow_file(not_object)


def test_parse_workflow_file_supports_yaml(tmp_path: pathlib.Path):
    import yaml

    path = tmp_path / "workflow.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "yml-demo",
                "trigger": {"type": "webhook", "config": {}},
                "steps": [{"name": "l", "type": "log", "message": "hi"}],
            }
        ),
        encoding="utf-8",
    )
    assert parse_workflow_file(path)["name"] == "yml-demo"


def test_workflow_slugs_are_safe():
    assert workflow_slug("my-workflow_v2") == "my-workflow_v2"
    assert workflow_slug("bad name!") == "bad_name_"


def test_shipped_examples_are_valid(store: Store):
    for example in sorted(EXAMPLES.glob("*.json")):
        definition = parse_workflow_file(example)
        validate_workflow(definition)
        saved = store.save_workflow(definition)
        assert saved["row"]["name"] == definition["name"]
