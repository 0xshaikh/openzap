"""``write_file`` action: write the rendered payload to a file on disk.

Config
------
``path``     Destination path (required). Relative paths are resolved against
             the data directory so workflows cannot wander into ``/etc``.
``data``     Content to write; defaults to the whole trigger body.
``append``   Default ``false``; ``true`` appends (and adds a newline when
             the payload is not already newline-terminated).
``pretty``   Pretty-print JSON dicts/lists (default ``true``).
``encoding`` Default ``utf-8``.

Result
------
``{"path": <absolute path>, "bytes_written": n, "append": bool}``
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from openzap.errors import ActionError


def _resolve_path(settings: Any, path: str) -> pathlib.Path:
    """Resolve ``path``, sandboxing relative paths inside the data directory."""
    target = pathlib.Path(path)
    if target.is_absolute():
        return target.expanduser()
    home = pathlib.Path(settings.home) if settings is not None else pathlib.Path(".")
    return (home / target).expanduser()


def _encode(data: Any, pretty: bool, encoding: str, compact: bool = False) -> bytes:
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, str):
        return data.encode(encoding)
    # ``compact`` produces a single line so ``append`` mode yields valid JSONL.
    indent = None if compact else (2 if pretty else None)
    return json.dumps(data, indent=indent, ensure_ascii=False, default=str).encode(encoding)


def run(config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Write rendered data to disk and report the path and byte count."""
    raw_path = config.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ActionError("'write_file' requires a 'path'")

    settings = context.get("_settings")
    target = _resolve_path(settings, raw_path)
    append = bool(config.get("append"))

    if config.get("data") is not None:
        data = config["data"]
    else:
        trigger = context.get("trigger") or {}
        data = trigger.get("body", trigger) if isinstance(trigger, dict) else trigger

    try:
        payload = _encode(
            data,
            bool(config.get("pretty", True)),
            str(config.get("encoding") or "utf-8"),
            compact=append,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ActionError(f"Cannot encode write_file payload: {exc}") from exc

    try:
        if append and payload and not payload.endswith(b"\n"):
            payload += b"\n"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("ab" if append else "wb") as handle:
            handle.write(payload)
    except OSError as exc:
        raise ActionError(f"Cannot write to {target}: {exc}") from exc

    return {"path": str(target), "bytes_written": len(payload), "append": append}
