"""Shared exception types for OpenZap.

Keeping these in one module avoids circular imports between the template
engine, the action registry and the execution engine.
"""

from __future__ import annotations


class OpenZapError(Exception):
    """Base class for every error raised by OpenZap."""


class WorkflowError(OpenZapError):
    """A workflow definition failed validation or could not be stored."""


class TemplateError(OpenZapError):
    """A ``{{ placeholder }}`` could not be resolved against the run context."""


class ActionError(OpenZapError):
    """An action failed while executing.

    ``result`` optionally carries the partial output of the failed action
    (for example an HTTP status code and a body excerpt) so the run log can
    still record what the remote side returned before the failure.
    """

    def __init__(self, message: str, result: dict | None = None) -> None:
        super().__init__(message)
        self.result = result or {}
