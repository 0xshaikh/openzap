"""Safe, dependency-free template substitution for workflow steps.

Placeholders look like ``{{ trigger.body.email }}`` and are resolved against a
flat context dictionary. Resolution only walks plain ``dict`` and ``list``
objects -- there is no attribute access, no ``eval`` and no access to builtins
or the Python object model -- so a template can never execute code.

Supported syntax
----------------
``{{ path.to.value }}``
    Dotted lookup through dictionaries. Dashes and spaces in a segment fall
    back to their ``_``-normalised form, so ``trigger.headers.x_api_key``
    matches a header named ``x-api-key``.

``{{ items[0].id }}`` and ``{{ items.0.id }}``
    Integer index into a list.

``{{ headers["X-Request-Id"] }}``
    Quoted key for names containing characters outside ``[A-Za-z0-9_-]``.

``{{ env.SMTP_FROM }}``
    Values from ``OPENZAP_*`` / ``SMTP_*`` environment variables.

``{{ now.iso }}``
    ISO-8601 UTC timestamp of the run.

If a whole string consists of exactly one placeholder the resolved value keeps
its native type, so ``"body": "{{ trigger.body }}"`` forwards the parsed JSON
object itself instead of a JSON-encoded string. Inside a longer string values
are stringified (``None`` becomes ``""``, booleans become ``true`` / ``false``,
dicts and lists are JSON-encoded).

Missing keys raise :class:`~openzap.errors.TemplateError`; the engine records
that as a failed step rather than silently substituting an empty string.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from openzap.errors import TemplateError
from openzap.util import utcnow_iso


_PLACEHOLDER = re.compile(r"\{\{\s*(?P<expr>[^{}]+?)\s*\}\}")

# One segment of a placeholder path: `['name']`, `["name"]`, `[1]`, `.name`,
# `.1` or the leading (dotless) name. The quoted-key alternative must come
# first so its opening quote is group 1 for the closing-quote backreference.
_SEGMENT = re.compile(
    r"""
      \[ \s* (?P<q>['"]) (?P<qkey>[^\1]*) \1 \s* \]  # ['weird-key']
    | \[ \s* (?P<idx>\d+) \s* \]                     # [0]
    | \. \s* (?P<attr>[A-Za-z_][A-Za-z0-9_\-]*)      # .body
    | \. \s* (?P<dotidx>\d+)                         # .0
    | (?P<bare>[A-Za-z_][A-Za-z0-9_\-]*)             # trigger
    """,
    re.VERBOSE,
)


def _get_key(container: Any, key: str, expr: str) -> Any:
    """Look ``key`` up in ``container`` with tolerant normalisation.

    Keys are compared exactly first, then case-sensitively against a
    ``-``/space-normalised form of the stored keys, so a header stored as
    ``x-api-key`` is reachable from a path segment ``x_api_key``.
    """
    if isinstance(container, dict):
        if key in container:
            return container[key]
        for stored, value in container.items():
            if stored.replace("-", "_").replace(" ", "_") == key:
                return value
        raise TemplateError(f"Key {key!r} not found at placeholder path {expr!r}")
    if isinstance(container, (list, tuple)):
        raise TemplateError(
            f"Cannot look up key {key!r} on a list at placeholder path {expr!r}"
        )
    raise TemplateError(
        f"Cannot look up key {key!r} on {type(container).__name__} "
        f"at placeholder path {expr!r}"
    )


def resolve(expr: str, context: dict[str, Any]) -> Any:
    """Resolve a placeholder expression against ``context`` and return the value.

    Raises :class:`~openzap.errors.TemplateError` when the path is malformed,
    a key does not exist, or the path reaches into Python internals.
    """
    expr = (expr or "").strip()
    if not expr:
        raise TemplateError("Empty placeholder expression")

    current: Any = context
    position = 0
    for match in _SEGMENT.finditer(expr):
        if match.start() != position:
            raise TemplateError(f"Malformed placeholder path {expr!r}")
        position = match.end()

        if match.group("q") is not None:
            if current is None:
                raise TemplateError(f"Cannot index into None at placeholder path {expr!r}")
            current = _get_key(current, match.group("qkey"), expr)
        elif match.group("idx") is not None or match.group("dotidx") is not None:
            index = int(match.group("idx") if match.group("idx") is not None else match.group("dotidx"))
            if current is None:
                raise TemplateError(f"Cannot index into None at placeholder path {expr!r}")
            if not isinstance(current, (list, tuple)):
                raise TemplateError(
                    f"Cannot index into {type(current).__name__} at placeholder path {expr!r}"
                )
            if index < 0:
                raise TemplateError("Negative list indices are not allowed")
            try:
                current = current[index]
            except IndexError as exc:
                raise TemplateError(
                    f"Index {index} out of range at placeholder path {expr!r}"
                ) from exc
        elif match.group("bare") is not None:
            segment = match.group("bare")
            if segment.startswith("_"):
                raise TemplateError(
                    f"Reserved placeholder name {segment!r} is not addressable"
                )
            current = _get_key(current, segment, expr)
        else:
            segment = match.group("attr")
            if current is None:
                raise TemplateError(f"Cannot index into None at placeholder path {expr!r}")
            current = _get_key(current, segment, expr)

    if position != len(expr):
        raise TemplateError(f"Malformed placeholder path {expr!r}")
    return current


def stringify(value: Any) -> str:
    """Render a resolved value as text for interpolation inside a larger string."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _substitute(template: str, context: dict[str, Any]) -> str:
    """Replace placeholders in ``template`` with stringified values."""
    if "{{" not in template:
        return template
    return _PLACEHOLDER.sub(
        lambda m: stringify(resolve(m.group("expr"), context)), template
    )


def render_text(template: str, context: dict[str, Any]) -> str:
    """Substitute placeholders in ``template`` and always return a string."""
    if not isinstance(template, str):
        raise TypeError("render_text expects a string")
    return _substitute(template, context)


def render_value(value: Any, context: dict[str, Any]) -> Any:
    """Recursively render every string inside ``value``.

    Strings that are exactly one placeholder keep the native type of the
    resolved value. Nested dictionaries and lists are rendered element-wise so
    action configs such as ``headers`` or ``body`` can be templated wholesale.
    """
    if isinstance(value, str):
        if not _PLACEHOLDER.search(value):
            return value
        single = _PLACEHOLDER.fullmatch(value)
        if single:
            return resolve(single.group("expr"), context)
        return _substitute(value, context)
    if isinstance(value, dict):
        return {key: render_value(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [render_value(item, context) for item in value]
    return value


def build_context(
    *,
    workflow_name: str,
    workflow_description: str,
    trigger_payload: dict[str, Any],
    step_results: dict[str, Any],
    settings_env: dict[str, str],
) -> dict[str, Any]:
    """Assemble the lookup context handed to :func:`render_value`."""
    return {
        "trigger": trigger_payload,
        "steps": step_results,
        "workflow": {
            "name": workflow_name,
            "description": workflow_description,
        },
        "env": settings_env,
        "now": {"iso": utcnow_iso(), "unix": int(time.time())},
    }
