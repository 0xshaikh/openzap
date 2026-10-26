"""Workflow definition schema and validation.

A workflow is plain JSON (YAML is accepted when ``PyYAML`` is installed)::

    {
      "name": "webhook-to-slack",
      "description": "Forward incoming webhook payloads to Slack",
      "enabled": true,
      "stop_on_error": true,
      "trigger": {"type": "webhook", "config": {}},
      "steps": [
        {
          "name": "notify",
          "type": "slack_webhook",
          "url": "https://hooks.slack.com/services/...",
          "text": "New event from {{ trigger.body.email }}"
        }
      ]
    }

Validation is intentionally strict: a bad definition is rejected at load time
with a clear message instead of blowing up mid-run.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

from openzap.actions import ACTION_TYPES
from openzap.errors import WorkflowError

TRIGGER_TYPES = ("webhook", "schedule")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,98}$")


def parse_workflow_file(path: str | pathlib.Path) -> dict[str, Any]:
    """Load a workflow definition from a JSON (or YAML) file."""
    path = pathlib.Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise WorkflowError(
                f"{path}: YAML workflow files need PyYAML installed "
                "(`pip install PyYAML`); otherwise use JSON"
            ) from exc
        data = yaml.safe_load(text)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise WorkflowError(f"{path}: invalid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise WorkflowError(f"{path}: workflow root must be a JSON object")
    return data


def validate_workflow(definition: dict[str, Any]) -> dict[str, Any]:
    """Validate ``definition`` in place, adding defaults, and return it."""
    if not isinstance(definition, dict):
        raise WorkflowError("Workflow definition must be a JSON object")

    name = definition.get("name")
    if not isinstance(name, str) or not name.strip():
        raise WorkflowError("'name' is required and must be a non-empty string")
    name = name.strip()
    if not _NAME_RE.match(name):
        raise WorkflowError(
            f"Invalid workflow name {name!r}: use 1-99 chars of "
            "letters, digits, dot, underscore or dash, starting with a letter/digit"
        )
    definition["name"] = name

    description = definition.get("description", "")
    if not isinstance(description, str):
        raise WorkflowError("'description' must be a string")
    definition["description"] = description[:500]

    enabled = definition.get("enabled", True)
    if not isinstance(enabled, bool):
        raise WorkflowError("'enabled' must be a boolean")
    definition["enabled"] = enabled

    stop_on_error = definition.get("stop_on_error", True)
    if not isinstance(stop_on_error, bool):
        raise WorkflowError("'stop_on_error' must be a boolean")
    definition["stop_on_error"] = stop_on_error

    trigger = definition.get("trigger")
    if not isinstance(trigger, dict):
        raise WorkflowError("'trigger' is required and must be an object")
    trigger_type = trigger.get("type")
    if trigger_type not in TRIGGER_TYPES:
        raise WorkflowError(
            f"Unknown trigger type {trigger_type!r}; expected one of {TRIGGER_TYPES}"
        )
    trigger.setdefault("config", {})
    if not isinstance(trigger["config"], dict):
        raise WorkflowError("trigger.config must be an object")

    if trigger_type == "schedule":
        cron = str(trigger["config"].get("cron", "")).strip()
        if not cron:
            raise WorkflowError("schedule trigger requires config.cron, e.g. '*/5 * * * *'")
        trigger["config"]["cron"] = cron
        _check_cron(cron)
    else:  # webhook
        path = trigger["config"].get("path")
        if path is not None and not isinstance(path, str):
            raise WorkflowError("trigger.config.path must be a string")

    steps = definition.get("steps")
    if not isinstance(steps, list) or not steps:
        raise WorkflowError("'steps' must be a non-empty list of actions")

    seen: set[str] = set()
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise WorkflowError(f"steps[{index}] must be an object")
        action_type = step.get("type")
        if action_type not in ACTION_TYPES:
            raise WorkflowError(
                f"steps[{index}]: unknown action type {action_type!r}; "
                f"expected one of {sorted(ACTION_TYPES)}"
            )
        step_name = step.get("name") or f"step_{index}"
        if not isinstance(step_name, str) or not step_name.strip():
            raise WorkflowError(f"steps[{index}].name must be a non-empty string")
        step_name = step_name.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_\-]{0,63}$", step_name):
            raise WorkflowError(
                f"steps[{index}].name {step_name!r} is invalid: start with a letter or "
                "underscore, then letters, digits, dashes or underscores"
            )
        if step_name in seen:
            raise WorkflowError(f"Duplicate step name {step_name!r}")
        seen.add(step_name)
        step["name"] = step_name
        if action_type == "email" and not step.get("to"):
            raise WorkflowError(f"step {step_name!r}: 'email' action requires a 'to' field")

    definition["steps"] = steps
    definition["schema_version"] = definition.get("schema_version", 1)
    return definition


def _check_cron(expression: str) -> None:
    """Raise :class:`WorkflowError` when ``expression`` is not a valid 5-field cron."""
    parts = expression.split()
    if len(parts) != 5:
        raise WorkflowError(
            f"Invalid cron expression {expression!r}: expected 5 fields "
            "(minute hour day-of-month month day-of-week)"
        )
    try:
        from apscheduler.triggers.cron import CronTrigger

        CronTrigger.from_crontab(expression)
    except Exception as exc:  # pragma: no cover - APScheduler messages vary
        raise WorkflowError(f"Invalid cron expression {expression!r}: {exc}") from exc


def workflow_slug(name: str) -> str:
    """File-system-safe filename stem for a workflow name."""
    return re.sub(r"[^A-Za-z0-9._\-]", "_", name)
