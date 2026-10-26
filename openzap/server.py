"""FastAPI application: webhook endpoints, scheduler and JSON API.

Run with ``openzap serve --port 8080``. The same ``create_app`` factory is used
by the pytest suite via ``fastapi.testclient.TestClient``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from openzap.config import Settings
from openzap.errors import WorkflowError
from openzap.storage import Store
from openzap import triggers
from openzap.triggers import (
    build_manual_payload,
    build_webhook_payload,
    run_workflow_by_name,
)

LOGGER = logging.getLogger("openzap.server")


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    """Build the OpenZap FastAPI application.

    Parameters
    ----------
    settings:
        Optional :class:`Settings`; defaults to environment-derived settings.
    store:
        Optional pre-built :class:`Store` (used by tests).
    """
    settings = settings or Settings()
    store = store or Store(settings)

    app = FastAPI(
        title="OpenZap",
        description="Self-hosted workflow automation: wire triggers to actions.",
        version="0.1.0",
    )
    app.state.settings = settings
    app.state.store = store
    app.state.scheduler = None

    # ------------------------------------------------------------- API routes
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        """Liveness probe."""
        return {"status": "ok", "service": "openzap"}

    @app.get("/api/workflows")
    def workflows() -> dict[str, Any]:
        """List workflows with their webhook URLs and latest run status."""
        items = []
        for row in store.list_workflows():
            item = dict(row)
            if row.get("trigger_type") == "webhook":
                item["hook_url"] = f"{settings.webhook_base()}/api/hooks/{row.get('hook_token')}"
            item["last_run"] = store.last_run(row["name"])
            items.append(item)
        return {"workflows": items}

    @app.get("/api/workflows/{name}")
    def workflow(name: str) -> dict[str, Any]:
        """Fetch one workflow definition."""
        entry = store.get_workflow(name)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"Unknown workflow {name!r}")
        return {
            "row": entry["row"],
            "definition": entry["definition"],
            "hook_url": store.webhook_url(name),
        }

    @app.post("/api/workflows")
    async def create_workflow(request: Request) -> dict[str, Any]:
        """Create or replace a workflow from a JSON body."""
        try:
            definition = await request.json()
            saved = store.save_workflow(definition)
        except WorkflowError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return saved

    @app.post("/api/workflows/{name}/run")
    async def run_workflow(request: Request, name: str) -> dict[str, Any]:
        """Manually fire a workflow with an arbitrary JSON trigger payload.

        The request body is exposed to steps as ``trigger.body`` so the same
        workflow can be driven by a webhook and by hand.
        """
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - empty body is legal
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object")
        try:
            return run_workflow_by_name(store, settings, name, build_manual_payload(body, "ui"))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/runs")
    def runs(workflow: str | None = None, limit: int = 100) -> dict[str, Any]:
        """Run history, newest first."""
        return {"runs": store.list_runs(workflow_name=workflow, limit=limit)}

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: int) -> dict[str, Any]:
        """One run with its per-step results."""
        record = store.get_run(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Unknown run {run_id}")
        return record

    @app.delete("/api/workflows/{name}")
    def delete_workflow(name: str) -> dict[str, Any]:
        """Delete a workflow definition (run history is kept)."""
        deleted = store.delete_workflow(name)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Unknown workflow {name!r}")
        return {"deleted": name}

    @app.post("/api/admin/reload")
    def reload_scheduler() -> dict[str, Any]:
        """Re-sync scheduled jobs from disk without restarting the process."""
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is None:
            return {"reloaded": False, "reason": "scheduler is not running"}
        from openzap.scheduler import sync_scheduler

        scheduled = sync_scheduler(scheduler, store, settings)
        return {"reloaded": True, "scheduled": scheduled}

    # ------------------------------------------------------------- webhooks
    @app.post("/api/hooks/{token}")
    async def webhook(token: str, request: Request) -> JSONResponse:
        """Fire the workflow whose webhook token matches ``{token}``."""
        entry = store.find_workflow_by_token(token)
        if entry is None:
            return JSONResponse({"error": "Unknown hook token"}, status_code=404)
        if not entry["row"].get("enabled"):
            return JSONResponse(
                {"error": f"Workflow {entry['row']['name']!r} is disabled"},
                status_code=403,
            )

        body_bytes = await request.body()
        if len(body_bytes) > settings.max_body_bytes:
            return JSONResponse(
                {"error": f"Request body exceeds {settings.max_body_bytes} bytes"},
                status_code=413,
            )

        config = entry["definition"].get("trigger", {}).get("config", {})
        secret = str(config.get("secret") or "")
        signature = request.headers.get("X-OpenZap-Signature")
        if secret and not triggers.verify_webhook_signature(
            secret, body_bytes, signature
        ):
            return JSONResponse({"error": "Invalid webhook signature"}, status_code=401)

        payload = build_webhook_payload(
            body_bytes=body_bytes,
            headers=dict(request.headers),
            query=dict(request.query_params),
        )
        report = run_workflow_by_name(
            store, settings, entry["row"]["name"], payload
        )
        return JSONResponse(
            {
                "status": report["status"],
                "workflow": entry["row"]["name"],
                "run_id": report["run_id"],
                "duration_ms": report["duration_ms"],
                "steps": [
                    {"name": s["name"], "type": s["type"], "status": s["status"]}
                    for s in report["steps"]
                ],
            }
        )

    # ------------------------------------------------------------- web UI
    from openzap.webui import register_web_ui

    register_web_ui(app, store, settings)

    return app


def _get_app() -> FastAPI:
    """Lazy factory for ``uvicorn openzap.server:_get_app --factory``."""
    return create_app()
