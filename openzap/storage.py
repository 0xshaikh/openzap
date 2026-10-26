"""Persistence layer: workflow JSON files on disk plus a SQLite index.

Workflows live as human-readable JSON files under ``<OPENZAP_HOME>/workflows``
so you can version them in git or edit them with any editor. SQLite indexes
them for fast lookups and stores every execution in the ``runs`` table.

Two storage surfaces share one database file, which is why every statement is
serialised by a re-entrant lock: the FastAPI request handlers, the APScheduler
thread pool and the CLI all touch the same :class:`Store` instance.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import threading
from typing import Any

from openzap.config import Settings
from openzap.errors import WorkflowError
from openzap.util import new_hook_token, utcnow_iso
from openzap.workflow import validate_workflow, workflow_slug

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    slug          TEXT NOT NULL UNIQUE,
    file_path     TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    trigger_type  TEXT NOT NULL,
    trigger_cron  TEXT NOT NULL DEFAULT '',
    hook_token    TEXT UNIQUE,
    schema_version INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_name TEXT NOT NULL,
    trigger_type  TEXT,
    status        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    input_json    TEXT,
    steps_json    TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_runs_workflow ON runs (workflow_name, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_workflows_token ON workflows (hook_token);
"""
class Store:
    """Filesystem + SQLite storage for workflows and run history."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.ensure_dirs()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(settings.db_path),
            timeout=30,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)

    # ---------------------------------------------------------------- helpers
    def close(self) -> None:
        """Close the underlying database connection."""
        with self._lock:
            self._conn.close()

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["enabled"] = bool(data.get("enabled"))
        return data

    # -------------------------------------------------------------- workflows
    def save_workflow(
        self,
        definition: dict[str, Any],
        *,
        keep_token: bool = False,
    ) -> dict[str, Any]:
        """Validate, write to disk and upsert the index row for a workflow.

        ``keep_token=True`` reuses the existing webhook token so published hook
        URLs do not break when a workflow is edited.
        """
        definition = validate_workflow(json.loads(json.dumps(definition)))
        with self._lock:
            row = self._conn.execute(
                "SELECT id, hook_token FROM workflows WHERE name = ?",
                (definition["name"],),
            ).fetchone()

            if row is not None:
                hook_token = row["hook_token"] if keep_token else new_hook_token()
            else:
                hook_token = new_hook_token()

            path = self.settings.workflows_dir / f"{workflow_slug(definition['name'])}.json"
            path.write_text(
                json.dumps(definition, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            now = utcnow_iso()
            if row is None:
                self._conn.execute(
                    """INSERT INTO workflows
                       (name, slug, file_path, description, enabled, trigger_type,
                        trigger_cron, hook_token, schema_version, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        definition["name"],
                        workflow_slug(definition["name"]),
                        str(path),
                        definition["description"],
                        int(definition["enabled"]),
                        definition["trigger"]["type"],
                        definition["trigger"]["config"].get("cron", ""),
                        hook_token,
                        definition.get("schema_version", SCHEMA_VERSION),
                        now,
                        now,
                    ),
                )
            else:
                self._conn.execute(
                    """UPDATE workflows SET slug = ?, file_path = ?, description = ?,
                       enabled = ?, trigger_type = ?, trigger_cron = ?, hook_token = ?,
                       schema_version = ?, updated_at = ? WHERE name = ?""",
                    (
                        workflow_slug(definition["name"]),
                        str(path),
                        definition["description"],
                        int(definition["enabled"]),
                        definition["trigger"]["type"],
                        definition["trigger"]["config"].get("cron", ""),
                        hook_token,
                        definition.get("schema_version", SCHEMA_VERSION),
                        now,
                        definition["name"],
                    ),
                )
        return self.get_workflow(definition["name"]) or {}

    def list_workflows(self) -> list[dict[str, Any]]:
        """All indexed workflows, most recently updated first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workflows ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_workflow(self, name: str) -> dict[str, Any] | None:
        """Return ``{'row': {...}, 'definition': {...}}`` for one workflow."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflows WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            return None
        row = self._row_to_dict(row)
        path = pathlib.Path(row["file_path"])
        if not path.exists():
            raise WorkflowError(
                f"Workflow {name!r} is indexed but {path} is missing"
            )
        definition = json.loads(path.read_text(encoding="utf-8"))
        return {"row": row, "definition": definition}

    def find_workflow_by_token(self, token: str) -> dict[str, Any] | None:
        """Resolve a webhook token to its workflow (for ``POST /api/hooks/<token>``)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflows WHERE hook_token = ?", (token,)
            ).fetchone()
        if row is None:
            return None
        row = self._row_to_dict(row)
        definition = json.loads(pathlib.Path(row["file_path"]).read_text(encoding="utf-8"))
        return {"row": row, "definition": definition}

    def delete_workflow(self, name: str) -> bool:
        """Remove a workflow from disk and the index. Returns True if it existed."""
        with self._lock:
            row = self._conn.execute(
                "SELECT file_path FROM workflows WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                return False
            self._conn.execute("DELETE FROM workflows WHERE name = ?", (name,))
        path = pathlib.Path(row["file_path"])
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return True

    def webhook_url(self, name: str) -> str | None:
        """Public URL for a workflow's webhook trigger, if it is a webhook."""
        entry = self.get_workflow(name)
        if entry is None or entry["row"].get("trigger_type") != "webhook":
            return None
        return f"{self.settings.webhook_base()}/api/hooks/{entry['row']['hook_token']}"

    # ------------------------------------------------------------------- runs
    def record_run(self, report: dict[str, Any], input_payload: Any) -> int:
        """Persist one execution report into the ``runs`` table; returns the id."""
        with self._lock:
            cursor = self._conn.execute(
                """INSERT INTO runs
                   (workflow_name, trigger_type, status, started_at, finished_at,
                    duration_ms, error, input_json, steps_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    report.get("workflow"),
                    report.get("trigger_type"),
                    report.get("status"),
                    report.get("started_at"),
                    report.get("finished_at"),
                    int(report.get("duration_ms") or 0),
                    report.get("error"),
                    json.dumps(input_payload, ensure_ascii=False, default=str),
                    json.dumps(report.get("steps", []), ensure_ascii=False, default=str),
                ),
            )
            run_id = cursor.lastrowid
            return int(run_id) if run_id is not None else 0

    def list_runs(
        self,
        workflow_name: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Newest runs first, optionally filtered to a single workflow."""
        limit = max(1, min(int(limit), 1000))
        if workflow_name:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM runs WHERE workflow_name = ? ORDER BY id DESC LIMIT ?",
                    (workflow_name, limit),
                ).fetchall()
        else:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [self._decode_run(r) for r in rows]

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        """Fetch one run by id."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE id = ?", (int(run_id),)
            ).fetchone()
        return self._decode_run(row) if row is not None else None

    def last_run(self, workflow_name: str) -> dict[str, Any] | None:
        """Most recent run for a workflow, used on the dashboard."""
        runs = self.list_runs(workflow_name=workflow_name, limit=1)
        return runs[0] if runs else None

    def _decode_run(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key in ("input_json", "steps_json"):
            try:
                data[key.replace("_json", "")] = json.loads(data.pop(key) or "null")
            except json.JSONDecodeError:
                data[key.replace("_json", "")] = None
        return data
