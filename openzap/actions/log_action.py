"""``log`` action: append one structured JSONL record to a log file.

Config
------
``file``    Output file; defaults to ``<OPENZAP_HOME>/logs/runs.jsonl``
``level``   Log level string (default ``info``)
``message`` Rendered message
``data``    Extra structured payload

Result
------
``{"file": <path>, "record": {...}}``
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from openzap.errors import ActionError
from openzap.util import utcnow_iso


def run(config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Append one JSON-lines record describing the current step."""
    settings = context.get("_settings")
    if settings is None:
        default_file = pathlib.Path("runs.jsonl")
    else:
        default_file = settings.logs_dir / "runs.jsonl"
    raw_file = config.get("file") or str(default_file)
    target = pathlib.Path(raw_file)
    if not target.is_absolute():
        home = settings.home if settings is not None else pathlib.Path(".")
        target = (pathlib.Path(home) / target).expanduser()

    workflow = context.get("workflow") or {}
    record = {
        "timestamp": utcnow_iso(),
        "workflow": workflow.get("name"),
        "step": config.get("name") or context.get("_step_name"),
        "level": str(config.get("level") or "info"),
        "message": config.get("message"),
        "data": config.get("data"),
    }

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        raise ActionError(f"Cannot write log entry to {target}: {exc}") from exc

    return {"file": str(target), "record": record}
