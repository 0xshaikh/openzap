"""Server-rendered web UI (Jinja2).

Routes added by :func:`register_web_ui` are mounted last in
:func:`openzap.server.create_app`, so the ``/api/*`` and ``/api/hooks/*``
handlers win over the page routes.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from openzap.config import Settings
from openzap.errors import WorkflowError
from openzap.storage import Store
from openzap.triggers import build_manual_payload, run_workflow_by_name

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def pretty_json(value: object) -> str:
    """Jinja filter: JSON with two-space indentation."""
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def register_web_ui(app: FastAPI, store: Store, settings: Settings) -> None:
    """Attach the dashboard, history and workflow editor routes."""
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["prettyjson"] = pretty_json
    templates.env.globals["root_path"] = ""

    if STATIC_DIR.is_dir():
        # Mounted last so /api/* and /api/hooks/* handlers keep precedence.
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        """List workflows, webhook URLs and their most recent run."""
        workflows = store.list_workflows()
        for row in workflows:
            if row.get("trigger_type") == "webhook":
                row["hook_url"] = f"{settings.webhook_base()}/api/hooks/{row.get('hook_token')}"
            row["last_run"] = store.last_run(row["name"])
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "active": "dashboard",
                "workflows": workflows,
                "enabled_count": sum(1 for w in workflows if w.get("enabled")),
                "run_count": len(store.list_runs(limit=1000)),
                "settings": settings,
            },
        )

    @app.get("/runs", response_class=HTMLResponse)
    def history(request: Request, workflow: str | None = None, limit: int = 50) -> HTMLResponse:
        """Filterable run history table."""
        return templates.TemplateResponse(
            request,
            "history.html",
            {
                "active": "history",
                "workflows": store.list_workflows(),
                "runs": store.list_runs(workflow_name=workflow, limit=limit),
                "filter": workflow or "",
                "limit": limit,
            },
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: int) -> HTMLResponse:
        """Single run with its trigger payload and per-step results."""
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Unknown run {run_id}")
        return templates.TemplateResponse(request, "run_detail.html", {"active": "history", "run": run})

    @app.get("/workflows/new", response_class=HTMLResponse)
    def workflow_new(request: Request) -> HTMLResponse:
        """Editor pre-filled with a starter workflow.

        Registered before ``/workflows/{name}`` so the literal path ``new``
        is not captured as a workflow name.
        """
        starter = {
            "name": "my-first-workflow",
            "description": "Forward an incoming webhook payload to a log file",
            "enabled": True,
            "stop_on_error": True,
            "trigger": {"type": "webhook", "config": {}},
            "steps": [
                {
                    "name": "write",
                    "type": "write_file",
                    "path": "incoming/events.jsonl",
                    "data": "{{ trigger.body }}",
                    "append": True,
                }
            ],
        }
        return templates.TemplateResponse(
            request,
            "workflow_editor.html",
            {
                "active": "editor",
                "title": "New workflow",
                "name": None,
                "definition": starter,
                "settings": settings,
                "error": None,
                "notice": None,
            },
        )

    @app.get("/workflows/{name}", response_class=HTMLResponse)
    def workflow_view(request: Request, name: str) -> HTMLResponse:
        """Workflow definition, webhook URL and recent runs."""
        entry = store.get_workflow(name)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"Unknown workflow {name!r}")
        return templates.TemplateResponse(
            request,
            "workflow_view.html",
            {
                "active": "dashboard",
                "row": entry["row"],
                "definition": entry["definition"],
                "hook_url": store.webhook_url(name),
                "runs": store.list_runs(workflow_name=name, limit=10),
            },
        )

    @app.get("/workflows/{name}/edit", response_class=HTMLResponse)
    def workflow_edit(request: Request, name: str) -> HTMLResponse:
        """Editor showing the current definition."""
        entry = store.get_workflow(name)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"Unknown workflow {name!r}")
        return templates.TemplateResponse(
            request,
            "workflow_editor.html",
            {
                "active": "editor",
                "title": f"Edit {name}",
                "name": name,
                "definition": entry["definition"],
                "settings": settings,
                "error": None,
                "notice": None,
            },
        )

    @app.post("/workflows/{name}/save")
    def workflow_save(request: Request, name: str, definition: str = Form(...)):
        """Validate and persist an edited workflow, then re-sync the scheduler."""
        try:
            data = json.loads(definition)
            store.save_workflow(data, keep_token=True)
        except json.JSONDecodeError as exc:
            return _redirect_to_editor(request, name, definition, f"Invalid JSON: {exc}")
        except WorkflowError as exc:
            return _redirect_to_editor(request, name, definition, f"Validation failed: {exc}")

        scheduler = getattr(request.app.state, "scheduler", None)
        if scheduler is not None:
            from openzap.scheduler import sync_scheduler

            sync_scheduler(scheduler, store, settings)
        return RedirectResponse(url=f"/workflows/{data.get('name', name)}", status_code=303)

    def _redirect_to_editor(
        request: Request, name: str, raw: str, error: str
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "workflow_editor.html",
            {
                "active": "editor",
                "title": f"Edit {name}" if name != "new" else "New workflow",
                "name": None if name == "new" else name,
                "definition": json.loads(raw) if _is_json(raw) else {"name": "my-first-workflow"},
                "settings": settings,
                "error": error,
                "notice": None,
            },
        )

    def _is_json(raw: str) -> bool:
        try:
            json.loads(raw)
            return True
        except json.JSONDecodeError:
            return False

    @app.post("/workflows/{name}/run")
    def workflow_run(request: Request, name: str, payload: str = Form("{}")) -> RedirectResponse:
        """Fire a workflow once from the browser."""
        try:
            trigger = build_manual_payload(json.loads(payload or "{}"), source="ui")
        except json.JSONDecodeError:
            trigger = build_manual_payload({"text": payload}, source="ui")
        try:
            run_workflow_by_name(store, settings, name, trigger)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Unknown workflow {name!r}") from None
        return RedirectResponse(url=f"/runs?workflow={name}", status_code=303)

    @app.post("/workflows/{name}/delete")
    def workflow_delete(name: str) -> RedirectResponse:
        """Delete a workflow definition."""
        if not store.delete_workflow(name):
            raise HTTPException(status_code=404, detail=f"Unknown workflow {name!r}")
        return RedirectResponse(url="/", status_code=303)

    return None
