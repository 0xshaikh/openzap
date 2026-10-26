"""Tests for the command-line interface."""

from __future__ import annotations

import json
import pathlib

import pytest

from openzap.cli import build_parser, main
from openzap.config import Settings
from openzap.storage import Store


def test_main_dispatches_list_and_show(store: Store, settings: Settings):
    store.save_workflow(
        {
            "name": "cli-demo",
            "description": "demo",
            "trigger": {"type": "webhook", "config": {}},
            "steps": [{"name": "l", "type": "log", "message": "hi"}],
        }
    )
    assert main(["--data-dir", str(settings.home), "list"]) == 0
    assert main(["--data-dir", str(settings.home), "show", "cli-demo"]) == 0
    assert main(["--data-dir", str(settings.home), "show", "missing"]) == 1


def test_main_run_wraps_input_as_trigger_body(capsys: pytest.CaptureFixture, store, settings):
    store.save_workflow(
        {
            "name": "runnable",
            "trigger": {"type": "webhook", "config": {}},
            "steps": [{"name": "l", "type": "log", "message": "got {{ trigger.body.email }}"}],
        }
    )
    code = main(
        ["--data-dir", str(settings.home), "run", "runnable", "--input", '{"email": "ada@example.com"}']
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "status:   success" in out
    assert "run_id:" in out
    runs = store.list_runs(workflow_name="runnable")
    assert runs[0]["input"]["body"] == {"email": "ada@example.com"}
    assert runs[0]["input"]["type"] == "manual"


def test_main_run_reports_failures(capsys: pytest.CaptureFixture, store, settings):
    store.save_workflow(
        {
            "name": "will-fail",
            "trigger": {"type": "webhook", "config": {}},
            "steps": [{"name": "l", "type": "log", "message": "{{ trigger.body.missing }}"}],
        }
    )
    code = main(["--data-dir", str(settings.home), "run", "will-fail", "--input", "{}"])
    assert code == 1
    assert "failed" in capsys.readouterr().out


def test_main_rejects_invalid_input_json(capsys: pytest.CaptureFixture, store, settings):
    store.save_workflow(
        {
            "name": "x",
            "trigger": {"type": "webhook", "config": {}},
            "steps": [{"name": "l", "type": "log", "message": "hi"}],
        }
    )
    assert main(["--data-dir", str(settings.home), "run", "x", "--input", "{not json"]) == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_main_webhook_url_and_validate(capsys: pytest.CaptureFixture, store, settings):
    store.save_workflow(
        {
            "name": "hooked",
            "trigger": {"type": "webhook", "config": {}},
            "steps": [{"name": "l", "type": "log", "message": "hi"}],
        }
    )
    assert main(["--data-dir", str(settings.home), "webhook-url", "hooked"]) == 0
    url = capsys.readouterr().out.strip()
    assert url.startswith("http://127.0.0.1:8080/api/hooks/")

    schedule_row = store.save_workflow(
        {
            "name": "cron-job",
            "trigger": {"type": "schedule", "config": {"cron": "* * * * *"}},
            "steps": [{"name": "l", "type": "log", "message": "hi"}],
        }
    )["row"]
    assert main(["--data-dir", str(settings.home), "webhook-url", "cron-job"]) == 1

    good = settings.home / "good.json"
    good.write_text(
        json.dumps(
            {
                "name": "hooked",
                "trigger": {"type": "webhook", "config": {}},
                "steps": [{"name": "l", "type": "log"}],
            }
        ),
        encoding="utf-8",
    )
    assert main(["--data-dir", str(settings.home), "validate", str(good)]) == 0

    bad = settings.home / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    assert main(["--data-dir", str(settings.home), "validate", str(bad)]) == 1
    assert "invalid" in capsys.readouterr().err


def test_main_import_then_delete(capsys: pytest.CaptureFixture, store, settings, tmp_path: pathlib.Path):
    path = tmp_path / "imported.json"
    path.write_text(
        json.dumps(
            {
                "name": "imported-flow",
                "trigger": {"type": "webhook", "config": {}},
                "steps": [{"name": "l", "type": "log", "message": "hi"}],
            }
        ),
        encoding="utf-8",
    )
    assert main(["--data-dir", str(settings.home), "import", str(path)]) == 0
    assert "imported-flow" in capsys.readouterr().out
    assert main(["--data-dir", str(settings.home), "delete", "imported-flow"]) == 0
    assert main(["--data-dir", str(settings.home), "delete", "imported-flow"]) == 1


def test_main_init_imports_samples_and_is_idempotent(tmp_path: pathlib.Path, capsys):
    home = tmp_path / "fresh"
    assert main(["--data-dir", str(home), "init"]) == 0
    settings = Settings(home=home)
    assert [row["name"] for row in Store(settings).list_workflows()] == ["webhook-to-slack"]

    assert main(["--data-dir", str(home), "init"]) == 0  # re-running must not duplicate
    assert len(Store(settings).list_workflows()) == 1


def test_main_history_prints_a_table(capsys: pytest.CaptureFixture, store, settings):
    store.save_workflow(
        {
            "name": "hist",
            "trigger": {"type": "webhook", "config": {}},
            "steps": [{"name": "l", "type": "log", "message": "hi"}],
        }
    )
    store.record_run(
        {
            "workflow": "hist",
            "trigger_type": "webhook",
            "status": "success",
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:00:01+00:00",
            "duration_ms": 5,
            "error": None,
            "steps": [],
        },
        {},
    )
    assert main(["--data-dir", str(settings.home), "history", "--limit", "10"]) == 0
    assert "hist" in capsys.readouterr().out


def test_parser_requires_a_subcommand():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
