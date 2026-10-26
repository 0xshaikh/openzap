"""Workflow execution engine.

Steps run strictly in the order they appear in the definition. Each step's
result is stored under ``steps.<name>`` in the template context so later steps
can reference earlier output (``{{ steps.lookup.response.status_code }}``).

``stop_on_error`` (default ``true``) halts on the first failed step; set it to
``false`` and remaining steps still run. Every execution returns a
:class:`RunReport` that the caller persists through
:meth:`openzap.storage.Store.record_run`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from openzap.actions import run_action
from openzap.errors import ActionError, OpenZapError
from openzap.util import utcnow_iso
from openzap.templates import build_context, render_value


@dataclass
class RunReport:
    """Outcome of one workflow execution."""

    workflow: str
    trigger_type: str
    status: str = "success"
    started_at: str = field(default_factory=utcnow_iso)
    finished_at: str | None = None
    duration_ms: int = 0
    error: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict form suitable for JSON serialisation and the run log."""
        return {
            "workflow": self.workflow,
            "trigger_type": self.trigger_type,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "steps": self.steps,
        }


def _step_config(step: dict[str, Any]) -> dict[str, Any]:
    """Extract the action config, keeping ``name`` and ``type`` separate."""
    config = {key: value for key, value in step.items() if key not in {"name", "type"}}
    config["name"] = step["name"]
    return config


def run_workflow(
    definition: dict[str, Any],
    trigger_payload: dict[str, Any],
    *,
    settings: Any = None,
    initial_steps: dict[str, Any] | None = None,
) -> RunReport:
    """Execute every step of ``definition`` against ``trigger_payload``.

    Parameters
    ----------
    definition:
        A validated workflow dict as produced by ``openzap.workflow.validate_workflow``.
    trigger_payload:
        The trigger envelope, e.g. ``{"type": "webhook", "body": {...},
        "headers": {...}, "query": {...}}``.
    settings:
        :class:`openzap.config.Settings`; actions that need process settings
        (SMTP, log paths) read it from ``context["_settings"]``.
    initial_steps:
        Pre-seeded ``steps`` mapping, used when running a workflow a second
        time within the same context.

    Returns
    -------
    RunReport
        Always a report -- errors become a failed step plus a failed run.
    """
    name = definition.get("name", "unnamed")
    trigger_type = (definition.get("trigger") or {}).get("type", "")
    stop_on_error = bool(definition.get("stop_on_error", True))

    step_results: dict[str, Any] = dict(initial_steps or {})
    context = build_context(
        workflow_name=name,
        workflow_description=definition.get("description", ""),
        trigger_payload=trigger_payload,
        step_results=step_results,
        settings_env=settings.env_subset() if settings is not None else {},
    )
    context["_settings"] = settings

    report = RunReport(workflow=name, trigger_type=trigger_type)
    started = time.monotonic()

    for index, step in enumerate(definition.get("steps") or [], start=1):
        step_name = step.get("name") or f"step_{index}"
        step_type = step.get("type", "")
        step_started = time.monotonic()
        entry: dict[str, Any] = {
            "name": step_name,
            "type": step_type,
            "status": "success",
            "result": {},
            "error": None,
        }

        try:
            config = render_value(_step_config(step), context)
            context["_step_name"] = step_name
            result = run_action(step_type, config, context)
            step_results[step_name] = result
            context["steps"] = step_results
            entry["result"] = result
        except ActionError as exc:
            entry["status"] = "failed"
            entry["error"] = str(exc)
            if exc.result:
                entry["result"] = exc.result
                step_results[step_name] = exc.result
                context["steps"] = step_results
        except OpenZapError as exc:
            # Validation/template errors: the step could not even start.
            entry["status"] = "failed"
            entry["error"] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - the engine must never crash a run
            entry["status"] = "failed"
            entry["error"] = f"{type(exc).__name__}: {exc}"

        entry["duration_ms"] = int((time.monotonic() - step_started) * 1000)
        report.steps.append(entry)
        report.error = entry["error"]
        report.status = "failed" if entry["error"] else report.status

        if entry["error"] and stop_on_error:
            break

    report.duration_ms = int((time.monotonic() - started) * 1000)
    report.finished_at = utcnow_iso()
    return report
