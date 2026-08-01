"""Behavior tests for the Week 1 command seam."""

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _completed_payload(**overrides):
    """Canonical successful delegate_task response shape with overrides."""
    payload = {
        "results": [
            {
                "status": "completed",
                "summary": "the result",
                "api_calls": 1,
                "duration_seconds": 0.0,
            }
        ]
    }
    if overrides.get("results") is not None:
        payload["results"] = overrides["results"]
    return json.dumps(payload)


def load_tools():
    spec = importlib.util.spec_from_file_location(
        "nightshift_commands", ROOT / "nightshift_commands.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeContext:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def dispatch_tool(self, name, args):
        self.calls.append((name, args))
        return self.response


def test_nightshift_dispatches_one_leaf_goal_and_returns_summary():
    tools = load_tools()
    ctx = FakeContext(_completed_payload())

    assert tools.handle_nightshift(ctx, "  inspect the repo  ") == "the result"
    assert ctx.calls == [("delegate_task", {"goal": "inspect the repo", "role": "leaf"})]


def test_nightshift_requires_a_goal():
    tools = load_tools()
    ctx = FakeContext("")

    assert tools.handle_nightshift(ctx, "") == 'nightshift: usage: /nightshift "<goal>"'
    assert ctx.calls == []


def test_nightshift_surfaces_delegate_errors():
    tools = load_tools()
    ctx = FakeContext(json.dumps({"error": "missing parent agent"}))

    assert tools.handle_nightshift(ctx, "do work") == "nightshift: missing parent agent"


def test_nightshift_surfaces_non_completed_results():
    tools = load_tools()
    ctx = FakeContext(
        _completed_payload(results=[{"status": "interrupted", "summary": None}])
    )

    assert tools.handle_nightshift(ctx, "do work") == "nightshift: delegated task did not complete"


def test_nightshift_loads_delegate_task_module_before_dispatch():
    from nightshift_commands import _load_delegate_task_module

    _load_delegate_task_module()  # idempotent; just must not raise


def test_status_is_explicitly_deferred():
    tools = load_tools()

    assert tools.handle_nightshift_status(None, "") == "nightshift: status is not implemented yet (see Week 2)"
