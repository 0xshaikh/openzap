"""Command-line interface for OpenZap.

Commands
--------
``init``           Create the data directory and import a sample workflow
``list``           Show every workflow with its trigger and last run
``show NAME``      Print one workflow definition
``import FILE``    Validate and register a workflow JSON/YAML file
``run NAME``       Execute a workflow once with an optional ``--input`` payload
``history``        Print the run log
``webhook-url``    Print the public webhook URL for a workflow
``validate FILE``  Check a workflow file without saving it
``delete NAME``    Remove a workflow
``serve``          Start the FastAPI app with webhook routes and cron scheduler
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

from openzap import __version__
from openzap.config import Settings, load_settings
from openzap.errors import OpenZapError
from openzap.storage import Store
from openzap.workflow import parse_workflow_file, validate_workflow

SAMPLE_WORKFLOWS: list[dict[str, Any]] = [
    {
        "name": "webhook-to-slack",
        "description": "Forward an incoming webhook payload to a Slack channel",
        "enabled": True,
        "stop_on_error": True,
        "trigger": {"type": "webhook", "config": {}},
        "steps": [
            {
                "name": "notify",
                "type": "slack_webhook",
                "url": "https://hooks.slack.com/services/YOUR/WEBHOOK/URL",
                "text": "New event from {{ trigger.body.email }}",
            },
            {
                "name": "audit",
                "type": "log",
                "level": "info",
                "message": "notified about {{ trigger.body.email }}",
                "data": "{{ trigger.body }}",
            },
        ],
    }
]


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser."""
    parser = argparse.ArgumentParser(
        prog="openzap",
        description="OpenZap - a lightweight, self-hosted workflow automation engine.",
    )
    parser.add_argument("--version", action="version", version=f"openzap {__version__}")
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Data directory (default: $OPENZAP_HOME or ~/.openzap)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="Create the data directory and import sample workflows")
    p.add_argument("--no-examples", action="store_true", help="Skip importing sample workflows")

    sub.add_parser("list", help="List workflows")

    p = sub.add_parser("show", help="Print a workflow definition")
    p.add_argument("name")

    p = sub.add_parser("import", help="Validate and register a workflow file")
    p.add_argument("file")

    p = sub.add_parser("run", help="Run a workflow once")
    p.add_argument("name")
    p.add_argument("--input", default="{}", help="JSON trigger payload (default: {})")

    p = sub.add_parser("history", help="Show recorded runs")
    p.add_argument("--workflow", default=None)
    p.add_argument("--limit", type=int, default=25)

    p = sub.add_parser("webhook-url", help="Print the webhook URL for a workflow")
    p.add_argument("name")

    p = sub.add_parser("validate", help="Validate a workflow file without saving")
    p.add_argument("file")

    p = sub.add_parser("delete", help="Delete a workflow")
    p.add_argument("name")

    p = sub.add_parser("serve", help="Start the web server, webhook endpoints and scheduler")
    p.add_argument("--host", default=None, help="Bind address (default: $OPENZAP_HOST or 127.0.0.1)")
    p.add_argument("--port", type=int, default=None, help="Bind port (default: $OPENZAP_PORT or 8080)")
    p.add_argument("--reload", action="store_true", help="Restart the server on source changes")
    return parser


# --------------------------------------------------------------------- output


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    """Print a fixed-width plain-text table to stdout."""
    if not rows:
        print("(none)")
        return
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


def _status_pill(status: str) -> str:
    """ANSI-coloured status label for terminal output."""
    if os.environ.get("NO_COLOR"):
        return status
    colour = {"success": "\033[32m", "failed": "\033[31m"}.get(status, "\033[36m")
    return f"{colour}{status}\033[0m"


# -------------------------------------------------------------------- commands


def _settings_from_args(args: argparse.Namespace) -> Settings:
    return load_settings(args.data_dir)


def cmd_init(args: argparse.Namespace, settings: Settings) -> int:
    settings.ensure_dirs()
    store = Store(settings)
    created = 0
    if not args.no_examples:
        for definition in SAMPLE_WORKFLOWS:
            existing = store.get_workflow(definition["name"])
            if existing is None:
                store.save_workflow(definition)
                created += 1
    print(f"Data directory: {settings.home}")
    print(f"Database:       {settings.db_path}")
    print(f"Webhook base:   {settings.webhook_base()}")
    if created:
        print(f"Imported {created} sample workflow(s).")
    print()
    print("Next steps:")
    print("  openzap list")
    print(f"  openzap serve --port {settings.port}")
    print(f"  open your browser at {settings.webhook_base()}")
    return 0


def cmd_list(args: argparse.Namespace, settings: Settings) -> int:
    store = Store(settings)
    rows = []
    for row in store.list_workflows():
        trigger = row["trigger_cron"] if row["trigger_type"] == "schedule" else "webhook"
        last = store.last_run(row["name"])
        rows.append(
            [
                row["name"],
                trigger,
                "yes" if row["enabled"] else "no",
                _status_pill(last["status"]) if last else "-",
                f"{last['duration_ms']} ms" if last else "-",
            ]
        )
    _print_table(["NAME", "TRIGGER", "ENABLED", "LAST RUN", "DURATION"], rows)
    return 0


def cmd_show(args: argparse.Namespace, settings: Settings) -> int:
    store = Store(settings)
    entry = store.get_workflow(args.name)
    if entry is None:
        print(f"Unknown workflow {args.name!r}", file=sys.stderr)
        return 1
    print(json.dumps(entry["definition"], indent=2))
    return 0


def cmd_import(args: argparse.Namespace, settings: Settings) -> int:
    store = Store(settings)
    try:
        definition = parse_workflow_file(args.file)
        saved = store.save_workflow(definition)
    except OpenZapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Imported workflow {saved['row']['name']!r} -> {saved['row']['file_path']}")
    url = store.webhook_url(saved["row"]["name"])
    if url:
        print(f"Webhook URL: {url}")
    return 0


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    store = Store(settings)
    try:
        payload = json.loads(args.input)
    except json.JSONDecodeError as exc:
        print(f"error: --input is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("error: --input must be a JSON object", file=sys.stderr)
        return 2

    from openzap.triggers import build_manual_payload, run_workflow_by_name

    try:
        report = run_workflow_by_name(
            store, settings, args.name, build_manual_payload(payload, source="cli")
        )
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"workflow: {report['workflow']}")
    print(f"status:   {_status_pill(report['status'])}")
    print(f"duration: {report['duration_ms']} ms")
    print(f"run_id:   {report['run_id']}")
    for step in report["steps"]:
        label = step["status"].upper()
        line = f"  [{label:7}] {step['name']} ({step['type']}) {step['duration_ms']} ms"
        if step["error"]:
            line += f"\n            {step['error']}"
        print(line)
    return 0 if report["status"] == "success" else 1


def cmd_history(args: argparse.Namespace, settings: Settings) -> int:
    store = Store(settings)
    rows = []
    for run in store.list_runs(workflow_name=args.workflow, limit=args.limit):
        error = run.get("error") or ""
        rows.append(
            [
                str(run["id"]),
                run["workflow_name"],
                _status_pill(run["status"]),
                run["started_at"],
                f"{run['duration_ms']} ms",
                (error[:40] + "…") if len(error) > 40 else error,
            ]
        )
    _print_table(["ID", "WORKFLOW", "STATUS", "STARTED (UTC)", "DURATION", "ERROR"], rows)
    return 0


def cmd_webhook_url(args: argparse.Namespace, settings: Settings) -> int:
    store = Store(settings)
    url = store.webhook_url(args.name)
    if url is None:
        print(
            f"error: {args.name!r} is not a webhook-triggered workflow (or does not exist)",
            file=sys.stderr,
        )
        return 1
    print(url)
    return 0


def cmd_validate(args: argparse.Namespace, settings: Settings) -> int:
    try:
        definition = parse_workflow_file(args.file)
        validate_workflow(definition)
    except OpenZapError as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return 1
    print(f"valid: {args.file} ({len(definition.get('steps', []))} step(s))")
    return 0


def cmd_delete(args: argparse.Namespace, settings: Settings) -> int:
    store = Store(settings)
    if store.delete_workflow(args.name):
        print(f"Deleted workflow {args.name!r}")
        return 0
    print(f"Unknown workflow {args.name!r}", file=sys.stderr)
    return 1


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    host = args.host or settings.host
    port = args.port or settings.port
    store = Store(settings)

    if args.reload:
        print(f"OpenZap serving on http://{host}:{port} (auto-reload enabled)")
        uvicorn.run(
            "openzap.server:create_app",
            factory=True,
            host=host,
            port=port,
            reload=True,
            log_level="info",
        )
        return 0

    from openzap.server import create_app
    from openzap.scheduler import start_scheduler

    app = create_app(settings, store)
    app.state.scheduler = start_scheduler(store, settings)
    print(f"OpenZap serving on http://{host}:{port}")
    print(f"Workflows dir: {settings.workflows_dir}")
    print(f"Webhook URL prefix: {settings.webhook_base()}/api/hooks/<token>")
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


COMMANDS = {
    "init": cmd_init,
    "list": cmd_list,
    "show": cmd_show,
    "import": cmd_import,
    "run": cmd_run,
    "history": cmd_history,
    "webhook-url": cmd_webhook_url,
    "validate": cmd_validate,
    "delete": cmd_delete,
    "serve": cmd_serve,
}


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (``openzap`` console script)."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    settings = _settings_from_args(args)
    try:
        return COMMANDS[args.command](args, settings)
    except OpenZapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
