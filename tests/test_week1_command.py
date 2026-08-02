"""Regression tests for the /nightshift command seam.

These tests locked the Week 1 contract and have been kept up to date as
the contract moved in Week 2:

  * The handler signature changed from ``(ctx, raw_args)`` to ``(raw_args)``
    — the framework now binds the plugin context at register time and
    handlers read it back through a module helper.
  * The handler return value is now a structured ``"nightshift: ..."``
    envelope (either ``dispatched <task_id> ...`` for the async path or
    ``inline-completed ...`` for sync-fallback) rather than the bare
    delegated summary line that Week 1 produced.
  * The sibling-loading path no longer assumes the parent package
    ``hermes_plugins.nightshift`` is on ``sys.modules`` (Week 1's
    ``_load_delegate_task_module`` helper was inlined into the dispatch
    path under a try/except), and the file is loaded bare by these
    tests to exercise that fallback explicitly.

The test names retain their ``test_week1_*`` prefix because the intent
they lock down is the same as in Week 1: the ``/nightshift`` command
must (1) dispatch exactly one leaf ``delegate_task`` per call, (2)
reject an empty goal with a usage hint, (3) surface a delegate error
string to the chat, (4) surface a non-completed (interrupted) result
as a human-readable failure notice, and (5) tolerate a missing core
``delegate_tool`` module without raising while dispatching.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _completed_payload(**overrides):
    """Canonical successful delegate_task response shape, with overrides.

    Mirrors ``tools.delegate_tool``'s inline-sync response when the
    core decides to fall back to a synchronous run (e.g. ``hermes chat
    -q``'s single-shot adapter). Week 2 surfaces this through the
    handler's ``inline-completed`` branch.
    """
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


def _dispatched_payload(task_id="deleg_abc"):
    """The Week 2 async-path response: the core has accepted the
    background dispatch and will deliver its summary later via the
    completion event handler."""
    return json.dumps({
        "status": "dispatched",
        "delegation_id": task_id,
    })


def _load_tools_fresh():
    """Spec-load ``nightshift_commands`` standalone, with no parent
    package on ``sys.modules``.

    Mirrors the original Week 1 test setup. The sibling-guard in
    ``nightshift_commands._import_sibling`` is what makes this still
    work after the Week 2 sibling-loading fix.
    """
    # Drop any cached spec-loaded module from a previous test so the
    # sibling-guard fallback chain always runs from scratch.
    sys.modules.pop("nightshift_commands", None)
    sys.modules.pop("nightshift_state", None)
    spec = importlib.util.spec_from_file_location(
        "nightshift_commands", ROOT / "nightshift_commands.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Mirror what the plugin loader does for non-package loads: clear
    # the parent so the sibling-guard takes the fallback path. This
    # is the regression surface we are guarding.
    module.__package__ = ""
    spec.loader.exec_module(module)
    return module


class _BoundContext:
    """Minimal stand-in for Hermes's ``PluginContext``.

    The handler reads it back through ``_ctx()`` (a module helper that
    returns ``_PLUGIN_CTX``); ``_bind_plugin_context`` is the entry
    point ``register()`` uses in production to wire it.
    """

    def __init__(self, response):
        self.response = response
        self.calls = []

    def dispatch_tool(self, name, args):
        self.calls.append((name, args))
        return self.response


def _bind(tools, response):
    ctx = _BoundContext(response)
    tools._bind_plugin_context(ctx)
    return ctx


# ---------------------------------------------------------------------------
# /nightshift dispatch contract
# ---------------------------------------------------------------------------

def test_nightshift_dispatches_one_leaf_goal_via_inline_sync_path():
    """When the core answers inline-sync (Week 1 sync-adapter
    behaviour), the handler must surface the delegated summary inside
    the ``inline-completed`` envelope. The Week 2 ``dispatch_tool``
    call shape stays exactly: ``delegate_task`` with ``role``,
    ``background``, and the trimmed goal — no extra metadata."""
    tools = _load_tools_fresh()
    ctx = _bind(tools, _completed_payload())

    out = tools.handle_nightshift("  inspect the repo  ")

    assert ctx.calls == [
        ("delegate_task", {"goal": "inspect the repo", "role": "leaf", "background": True})
    ]
    assert out.startswith("nightshift: inline-completed ")
    assert "  summary: the result" in out


def test_nightshift_requires_a_goal():
    """The handler must reject empty input with a usage hint *before*
    it makes any tool call. Empty goal = the framework should not see
    a delegate_task on its way through."""
    tools = _load_tools_fresh()
    ctx = _bind(tools, "")

    out = tools.handle_nightshift("")

    assert out == 'nightshift: usage: /nightshift "<goal>"'
    assert ctx.calls == []


def test_nightshift_surfaces_delegate_errors():
    tools = _load_tools_fresh()
    ctx = _bind(tools, json.dumps({"error": "missing parent agent"}))

    out = tools.handle_nightshift("do work")

    assert "missing parent agent" in out
    assert out.startswith("nightshift: ")


def test_nightshift_surfaces_non_completed_results():
    """A non-completed result (e.g. ``interrupted``) is reported with the
    upstream status and an empty summary line, NOT mistaken for a
    completed run. The Week 2 envelope preserves the raw status so the
    user can grep for it; we lock both halves."""
    tools = _load_tools_fresh()
    ctx = _bind(
        tools, _completed_payload(results=[{"status": "interrupted", "summary": None}])
    )

    out = tools.handle_nightshift("do work")

    assert "inline-completed" in out
    assert "status: interrupted" in out
    assert "summary: (none)" in out


def test_nightshift_dispatch_handles_missing_delegate_tool_module(monkeypatch):
    """If the core ``tools.delegate_tool`` module cannot be imported
    (e.g. the plugin was loaded outside a hermes runtime, or the
    platform changed module layout), dispatch must not raise. The
    try/except guard inside ``_delegate_dispatch`` lets the call
    fall through; the framework then returns ``Unknown tool:
    delegate_task`` and the handler surfaces that to the chat."""
    tools = _load_tools_fresh()
    ctx = _bind(tools, "Unknown tool: delegate_task")

    # Force ``from tools import delegate_tool`` to fail by clearing
    # the entry point module. The handler's own try/except around
    # that import must absorb the failure rather than letting it
    # escape into the dispatch path.
    monkeypatch.setitem(sys.modules, "tools", None)

    out = tools.handle_nightshift("do work")
    assert "Unknown tool: delegate_task" in out
