"""Action registry.

Each action is a ``run(config, context) -> dict`` function. ``config`` is the
step definition after template rendering (plus ``name``, the step's own name),
and ``context`` carries the trigger payload, previous step results and the
process ``Settings`` under the reserved key ``_settings``.

Actions report failures by raising
:class:`openzap.errors.ActionError`; attaching ``result=`` preserves the
partial output (status code, body excerpt, path written, ...) so the run log
still shows what happened.
"""

from __future__ import annotations

from typing import Any, Callable

from openzap.actions import (
    email_action,
    http_request,
    log_action,
    slack_webhook,
    write_file,
)

Action = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]

ACTIONS: dict[str, Action] = {
    "http_request": http_request.run,
    "email": email_action.run,
    "slack_webhook": slack_webhook.run,
    "write_file": write_file.run,
    "log": log_action.run,
}

ACTION_TYPES: tuple[str, ...] = tuple(sorted(ACTIONS))


def run_action(action_type: str, config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one action by name and return its result dict."""
    try:
        action = ACTIONS[action_type]
    except KeyError:
        raise ValueError(
            f"Unknown action type {action_type!r}; expected one of {sorted(ACTIONS)}"
        ) from None
    result = action(config, context)
    return result if isinstance(result, dict) else {"value": result}
