"""Tests for the template substitution engine."""

from __future__ import annotations

import pytest

from openzap.errors import TemplateError
from openzap.templates import build_context, render_text, render_value, resolve, stringify


def context(**overrides):
    """A representative run context for template tests."""
    base = {
        "trigger": {
            "type": "webhook",
            "body": {
                "email": "ada@example.com",
                "count": 3,
                "items": [{"id": 7}, {"id": 8}],
                "nested": {"deep": "value"},
            },
            "headers": {"x-api-key": "secret", "host": "example.com"},
            "query": {"page": "2"},
            "raw": "raw-body",
        },
        "steps": {"lookup": {"response": {"status_code": 204, "body": {"url": "https://x"}}}},
        "workflow": {"name": "capture", "description": "test"},
        "env": {"SMTP_FROM": "noreply@example.com"},
        "now": {"iso": "2026-01-01T00:00:00+00:00", "unix": 1767225600},
        "_settings": "internal-must-not-be-reachable",
    }
    base.update(overrides)
    return base


def test_resolve_dotted_path():
    assert resolve("trigger.body.email", context()) == "ada@example.com"


def test_resolve_normalises_dashes_in_keys():
    assert resolve("trigger.headers.x_api_key", context()) == "secret"


def test_resolve_quoted_key_with_weird_characters():
    assert resolve('trigger.headers["x-api-key"]', context()) == "secret"


def test_resolve_list_indices():
    assert resolve("trigger.body.items[0].id", context()) == 7
    assert resolve("trigger.body.items.1.id", context()) == 8


def test_resolve_nested_steps_reference():
    assert resolve("steps.lookup.response.status_code", context()) == 204


def test_resolve_env_and_now():
    assert resolve("env.SMTP_FROM", context()) == "noreply@example.com"
    assert resolve("now.iso", context()) == "2026-01-01T00:00:00+00:00"


def test_resolve_missing_key_raises():
    with pytest.raises(TemplateError, match="not found"):
        resolve("trigger.body.no_such_key", context())


def test_resolve_out_of_range_index_raises():
    with pytest.raises(TemplateError, match="out of range"):
        resolve("trigger.body.items[9].id", context())


def test_resolve_negative_index_rejected():
    with pytest.raises(TemplateError):
        resolve("trigger.body.items[-1].id", context())


def test_resolve_reserved_names_are_unreachable():
    with pytest.raises(TemplateError, match="Reserved"):
        resolve("_settings.smtp.host", context())


def test_resolve_cannot_escape_into_python_internals():
    # Attribute traversal on dict values is not supported: only dict/list lookups.
    with pytest.raises(TemplateError):
        resolve("trigger.__class__", context())


def test_resolve_malformed_path_raises():
    with pytest.raises(TemplateError, match="Malformed"):
        resolve("trigger.body...", context())


def test_render_text_interpolates_inside_larger_string():
    out = render_text("Hi {{ trigger.body.email }}", context())
    assert out == "Hi ada@example.com"


def test_render_text_missing_key_raises_instead_of_empty_string():
    with pytest.raises(TemplateError):
        render_text("Hi {{ trigger.body.nope }}", context())


def test_render_text_without_placeholders_is_returned_unchanged():
    assert render_text("plain text", context()) == "plain text"


def test_render_value_preserves_native_type_for_single_placeholder():
    assert render_value("{{ trigger.body.count }}", context()) == 3
    assert render_value("{{ trigger.body }}", context()) == context()["trigger"]["body"]


def test_render_value_recurses_through_dicts_and_lists():
    rendered = render_value(
        {"headers": {"X-Email": "{{ trigger.body.email }}"}, "tags": ["{{ trigger.body.email }}"]},
        context(),
    )
    assert rendered["headers"]["X-Email"] == "ada@example.com"
    assert rendered["tags"] == ["ada@example.com"]


def test_render_value_leaves_non_strings_alone():
    assert render_value({"n": 4, "ok": True, "none": None}, context()) == {
        "n": 4,
        "ok": True,
        "none": None,
    }


def test_stringify_handles_none_bools_and_structured_values():
    assert stringify(None) == ""
    assert stringify(True) == "true"
    assert stringify(False) == "false"
    assert stringify(3) == "3"
    assert stringify({"a": 1}) == '{"a": 1}'
    assert stringify([1, 2]) == "[1, 2]"


def test_build_context_shape():
    ctx = build_context(
        workflow_name="wf",
        workflow_description="desc",
        trigger_payload={"body": {}},
        step_results={},
        settings_env={},
    )
    assert set(ctx) == {"trigger", "steps", "workflow", "env", "now"}
    assert ctx["workflow"]["name"] == "wf"
    assert isinstance(ctx["now"]["unix"], int)


def test_arbitrary_expressions_are_not_evaluated():
    # A placeholder containing an expression is treated as a key path, not code.
    with pytest.raises(TemplateError):
        render_text("{{ 1/0 }}", context())
