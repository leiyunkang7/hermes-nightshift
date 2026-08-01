"""Handlers for the Week 1, synchronous ``/nightshift`` command."""

import json


def _error(message: str) -> str:
    """Return a user-facing error without exposing a traceback."""
    return f"nightshift: {message}"


def _summary_from_delegate(result: str) -> str:
    """Turn Hermes' delegate JSON response into the command's final output."""
    try:
        payload = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        # Keep compatibility with a custom/test registry that returns text.
        return str(result)

    if payload.get("error"):
        return _error(str(payload["error"]))

    results = payload.get("results") or []
    if not results:
        return _error("delegated task returned no result")

    task = results[0]
    if task.get("status") != "completed":
        return _error(task.get("error") or "delegated task did not complete")
    return str(task.get("summary") or "delegated task completed without a summary")


def _load_delegate_task_module() -> None:
    """Import ``tools.delegate_tool`` so its ``registry.register`` side-effect runs.

    The plugin loader only imports the plugin package; core tools are
    registered lazily on first model request. Without this import, the
    command would dispatch against an empty tool registry and fail with
    ``Unknown tool: delegate_task``. Importing the module is idempotent.
    """
    from tools import delegate_tool as _delegate_module  # noqa: F401


def handle_nightshift(ctx, raw_args: str) -> str:
    """Run one goal through Hermes' existing synchronous delegation path."""
    goal = (raw_args or "").strip()
    if not goal:
        return _error('usage: /nightshift "<goal>"')

    _load_delegate_task_module()
    result = ctx.dispatch_tool(
        "delegate_task",
        {"goal": goal, "role": "leaf"},
    )
    return _summary_from_delegate(result)


def handle_nightshift_status(ctx, raw_args: str) -> str:
    """Week 1 placeholder; status belongs to the later persistence slice."""
    return _error("status is not implemented yet (see Week 2)")
