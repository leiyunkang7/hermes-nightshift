"""Slash command handlers for `hermes-nightshift`.

Week 2 slice. Exposes five commands via `PluginContext.register_command`:

  - `/nightshift "<goal>"`       — dispatch a delegated task in the
                                   background and write run state
  - `/nightshift-status [id]`    — list all runs, or show one run
  - `/nightshift-tail <id>`      — last N lines of a run's transcript
  - `/nightshift-pause <id>`     — request a single subagent to stop at
                                   its next iteration boundary
  - `/nightshift-resume <id>`    — clear the pause flag and (TODO) rerun
                                   the goal; Week 2 only clears the
                                   local pause marker, the actual rerun
                                   pipeline lands in Week 3
  - `/nightshift-inject <id> "text"` — stage an instruction for the
                                   active subagent (see state.py for
                                   the Week 2 limitation)

`PluginContext` is bound at plugin-registration time and stored as a
module attribute so the handlers (which the framework calls with a
single `raw_args: str` argument) can reach back to
`ctx.dispatch_tool()` and `ctx.register_command()`.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import shlex
import sys
import threading
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Import the sibling module the hard way:
#
#   from nightshift_state import ...
#
# fails at module-load time when this file is exec'd by
# `importlib.util.spec_from_file_location` (the loader is Hermes's
# plugin discovery) because importlib's bootstrap does not look up
# `sys.modules['hermes_plugins.nightshift.nightshift_state']` for a
# bare `nightshift_state` import in spec-loaded siblings — and the
# top-level `nightshift_state` name is not in sys.path.
#
# `import nightshift_state` ALSO fails because importlib looks for
# `sys.modules['nightshift_state']` (no prefix), and the FileFinder
# walks sys.path which doesn't contain the plugin dir.
#
# The reliable path: use `importlib.import_module(<fully-qualified
# name>)` once, cache the module on a local, and read attributes from
# the local. The fully-qualified lookup walks sys.modules' `parent`
# prefix and finds the module we pre-registered in __init__.py.
_nightshift_state = importlib.import_module(
    "hermes_plugins.nightshift.nightshift_state"
)

# Re-export the state functions under their old `nightshift_state.X`
# names so the rest of the file can use them unchanged.
append_transcript_line = _nightshift_state.append_transcript_line
injections_dir = _nightshift_state.injections_dir
last_transcript_lines = _nightshift_state.last_transcript_lines
list_runs = _nightshift_state.list_runs
load_state = _nightshift_state.load_state
new_task_id = _nightshift_state.new_task_id
prune_stale_runs = _nightshift_state.prune_stale_runs
run_dir = _nightshift_state.run_dir
save_state = _nightshift_state.save_state
state_path = _nightshift_state.state_path
transcript_path = _nightshift_state.transcript_path
write_injection = _nightshift_state.write_injection
write_transcript_header = _nightshift_state.write_transcript_header

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Plugin context binding
# ---------------------------------------------------------------------------
# Set by __init__.py:register() so the per-command handlers (which the
# framework calls with a single `raw_args` string, not `(ctx, raw_args)`)
# can reach `ctx.dispatch_tool()` to invoke core tools. See
# hermes_cli/plugins.py:PluginContext.register_command — the docstring
# there says `fn(raw_args: str) -> str | None`, and cli.py:9971 confirms
# the dispatch site calls `plugin_handler(user_args)`. Until the framework
# grows a two-arg variant, binding ctx at register time is the only way
# for handlers to invoke other tools.

_PLUGIN_CTX: Any = None


def _bind_plugin_context(ctx: Any) -> None:
    """Store the live `PluginContext` for handler dispatch. Idempotent."""
    global _PLUGIN_CTX
    _PLUGIN_CTX = ctx


def _ctx() -> Any:
    """Return the live plugin context. May be None during early unit tests."""
    return _PLUGIN_CTX


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _error(message: str) -> str:
    return f"nightshift: {message}"


def _delegate_dispatch(goal: str, *, background: bool, parent_ctx: Any) -> str:
    """Call `delegate_task` via the live plugin context.

    The framework auto-injects the parent agent when it sees the
    dispatch, so the handler does not need to thread it through.
    Returns the raw JSON string the tool produced.
    """
    if parent_ctx is None:
        return json.dumps({"error": "plugin context not bound (run inside hermes)"})
    # Force the core's `tools.delegate_tool` module to import so its
    # registry.register side-effect runs. The plugin loader only
    # imports the plugin package; core tools are registered lazily on
    # first model request. Without this, dispatch would return
    # `Unknown tool: delegate_task`. Idempotent.
    try:
        from tools import delegate_tool as _delegate_module  # noqa: F401
    except Exception:
        pass
    return parent_ctx.dispatch_tool(
        "delegate_task",
        {"goal": goal, "role": "leaf", "background": background},
    )


def _parse_kv_args(raw: str) -> Dict[str, str]:
    """Parse `key=value` pairs (shell-style quoting) from the tail of a
    command. Returns an empty dict if nothing parseable.

    This is the common shape Week 3 commands will need; Week 2 only
    uses it for `inject`'s `--text=` escape hatch.
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        tokens = shlex.split(raw)
    except ValueError:
        return {}
    out: Dict[str, str] = {}
    for tok in tokens:
        if "=" in tok and not tok.startswith("="):
            k, _, v = tok.partition("=")
            out[k.strip("-")] = v
    return out


def _command_help(name: str) -> str:
    return {
        "nightshift": 'usage: /nightshift "<goal>"',
        "nightshift-status": "usage: /nightshift-status [task_id]",
        "nightshift-tail": "usage: /nightshift-tail <task_id> [n_lines]",
        "nightshift-pause": "usage: /nightshift-pause <task_id>",
        "nightshift-resume": "usage: /nightshift-resume <task_id>",
        "nightshift-inject": 'usage: /nightshift-inject <task_id> "new instructions"',
    }[name]


# ---------------------------------------------------------------------------
# /nightshift
# ---------------------------------------------------------------------------

def handle_nightshift(raw_args: str) -> str:
    """Dispatch a background delegated task and write run state."""
    goal = (raw_args or "").strip()
    if not goal:
        return _error('usage: /nightshift "<goal>"')

    task_id = new_task_id()
    save_state(task_id, {
        "status": "dispatching",
        "goal": goal[:500],
        "started_at": _now_iso(),
        "role": "leaf",
        "background": True,
    })
    write_transcript_header(task_id, goal, source_log=None)

    # Try to dispatch; the core's background path returns
    # {"status": "dispatched", "delegation_id": "deleg_xxxxxxxx"} on success.
    raw = _delegate_dispatch(goal, background=True, parent_ctx=_ctx())
    payload = _safe_json(raw)

    if not payload:
        save_state(task_id, {"status": "dispatch_error", "error": "core returned no JSON"})
        return _error(f"dispatch failed: {raw[:200] if isinstance(raw, str) else raw}")

    if payload.get("error"):
        save_state(task_id, {"status": "dispatch_error", "error": str(payload["error"])[:500]})
        return _error(str(payload["error"]))

    delegation_id = payload.get("delegation_id") or ""
    if payload.get("status") == "dispatched" and delegation_id:
        # The core's `live_transcripts` array, when present, holds the
        # path to the human-readable per-task log. Index 0 is ours.
        live_paths = payload.get("live_transcripts") or []
        live_path = live_paths[0] if live_paths else None
        save_state(task_id, {
            "status": "running",
            "delegation_id": delegation_id,
            "core_live_transcript": live_path,
        })
        if live_path:
            # Rewrite the header now we know the source path.
            write_transcript_header(task_id, goal, source_log=live_path)

        # Opportunistic retention sweep — cheap, no-op when under cap.
        try:
            prune_stale_runs()
        except Exception:
            pass

        return (
            f"nightshift: dispatched {task_id}\n"
            f"  goal: {goal[:160]}\n"
            f"  delegation_id: {delegation_id}\n"
            f"  transcript: {transcript_path(task_id)}\n"
            f"\n"
            f"  tail with: /nightshift-tail {task_id}\n"
            f"  pause with: /nightshift-pause {task_id}\n"
            f"  inject with: /nightshift-inject {task_id} \"new instructions\"\n"
        )

    # Core fell back to inline sync (one-shot runtimes); record what we
    # know and surface the consolidated summary.
    results = payload.get("results") or []
    summary = ""
    status = "completed"
    if results:
        first = results[0]
        summary = str(first.get("summary") or "")
        status = str(first.get("status") or "completed")
    save_state(task_id, {
        "status": status,
        "delegation_id": delegation_id or None,
        "summary": summary[:2000],
        "inline": True,
    })
    if summary:
        append_transcript_line(task_id, f"[{_hh_mm_ss()}] final | {summary[:400]}")
    return (
        f"nightshift: inline-completed {task_id} (status: {status})\n"
        f"  summary: {summary or '(none)'}"
    )


# ---------------------------------------------------------------------------
# /nightshift-status
# ---------------------------------------------------------------------------

def handle_nightshift_status(raw_args: str) -> str:
    args = (raw_args or "").strip()
    if args in {"-h", "--help", "help"}:
        return _error(_command_help("nightshift-status"))

    if args:
        rec = load_state(args)
        if rec is None:
            return _error(f"no such run: {args}")
        return _format_status(rec)

    runs = list_runs()
    if not runs:
        return "nightshift: no runs yet — try `/nightshift \"<goal>\"`"
    head = f"nightshift: {len(runs)} run(s) (newest first)\n"
    lines = [head]
    for rec in runs[:20]:
        lines.append(_format_status_one_line(rec))
    return "\n".join(lines)


def _format_status(rec: Dict[str, Any]) -> str:
    one = _format_status_one_line(rec)
    extras = []
    for key in ("summary", "error", "core_live_transcript", "delegation_id"):
        v = rec.get(key)
        if v:
            extras.append(f"  {key}: {str(v)[:200]}")
    return one + ("\n" + "\n".join(extras) if extras else "")


def _format_status_one_line(rec: Dict[str, Any]) -> str:
    tid = rec.get("task_id", "?")
    status = rec.get("status", "?")
    goal = (rec.get("goal") or "").strip().splitlines()[0]
    if len(goal) > 80:
        goal = goal[:77] + "..."
    started = rec.get("started_at", "?")
    return f"  {tid}  [{status:>14}]  {started}  {goal}"


# ---------------------------------------------------------------------------
# /nightshift-tail
# ---------------------------------------------------------------------------

def handle_nightshift_tail(raw_args: str) -> str:
    parts = (raw_args or "").strip().split()
    if not parts or parts[0] in {"-h", "--help", "help"}:
        return _error(_command_help("nightshift-tail"))
    task_id = parts[0]
    n = 40
    if len(parts) >= 2:
        try:
            n = max(1, min(400, int(parts[1])))
        except ValueError:
            return _error("n_lines must be a positive integer")
    return last_transcript_lines(task_id, n=n)


# ---------------------------------------------------------------------------
# /nightshift-pause
# ---------------------------------------------------------------------------

def handle_nightshift_pause(raw_args: str) -> str:
    parts = (raw_args or "").strip().split()
    if not parts or parts[0] in {"-h", "--help", "help"}:
        return _error(_command_help("nightshift-pause"))
    task_id = parts[0]
    rec = load_state(task_id)
    if rec is None:
        return _error(f"no such run: {task_id}")
    delegation_id = rec.get("delegation_id") or ""
    if rec.get("status") not in {"running", "dispatching"}:
        return _error(f"run {task_id} is {rec.get('status')!r}, nothing to pause")

    # Best-effort: ask the core to interrupt the child. The core keeps
    # a module-level active-subagent registry; if this run is no longer
    # there (process restart, child already finished) the lookup fails
    # and we just mark the local state.
    hit = _interrupt_core_subagent(delegation_id)
    save_state(task_id, {
        "status": "pausing" if hit else "pause_requested",
        "pause_requested_at": _now_iso(),
    })
    append_transcript_line(task_id, f"[{_hh_mm_ss()}] user  | pause requested ({'core interrupted' if hit else 'core not running — flag set'})")
    return (
        f"nightshift: pause requested for {task_id}\n"
        f"  delegation_id: {delegation_id or '(none)'}\n"
        f"  core hit: {'yes' if hit else 'no (run is no longer registered; the flag is set so the next dispatch can short-circuit)'}"
    )


# ---------------------------------------------------------------------------
# /nightshift-resume
# ---------------------------------------------------------------------------

def handle_nightshift_resume(raw_args: str) -> str:
    """Week 2 resume: clear the pause flag and mark the run as rerunnable.

    Week 3 will grow this into `dispatch_async_delegation_batch(rerun=...)`
    or a similar core-level continuation. The Week 2 contract is: we
    never *restart* the subagent from inside the plugin; the user issues
    a fresh `/nightshift` if they want a rerun. Resume here means
    "allow future dispatches to proceed (clear `pause_requested`) and
    mark this run as reschedulable."
    """
    parts = (raw_args or "").strip().split()
    if not parts or parts[0] in {"-h", "--help", "help"}:
        return _error(_command_help("nightshift-resume"))
    task_id = parts[0]
    rec = load_state(task_id)
    if rec is None:
        return _error(f"no such run: {task_id}")
    if rec.get("status") not in {"pausing", "pause_requested", "interrupted"}:
        return _error(
            f"run {task_id} is {rec.get('status')!r}; resume is for paused/interrupted runs only"
        )
    save_state(task_id, {"status": "resumable", "resumed_at": _now_iso()})
    append_transcript_line(task_id, f"[{_hh_mm_ss()}] user  | resume: run is reschedulable; issue a fresh `/nightshift` to rerun the goal")
    return (
        f"nightshift: {task_id} marked resumable\n"
        f"  Week 2: a *new* `/nightshift \"<goal>\"` will pick up the original goal; the\n"
        f"  past transcript and state stay on disk under the original task_id."
    )


# ---------------------------------------------------------------------------
# /nightshift-inject
# ---------------------------------------------------------------------------

def handle_nightshift_inject(raw_args: str) -> str:
    """Stage an instruction for the active subagent.

    See `nightshift_state.write_injection` for the Week 2 limitation:
    the core has no public mid-run subagent hook API, so we stage the
    text on disk. The transcript mirror gains a `[INJECT pending]` line
    so an operator watching the run sees the request immediately.
    """
    parts = (raw_args or "").strip().split(maxsplit=1)
    if not parts or parts[0] in {"-h", "--help", "help"}:
        return _error(_command_help("nightshift-inject"))
    task_id = parts[0]
    body = parts[1] if len(parts) > 1 else ""
    rec = load_state(task_id)
    if rec is None:
        return _error(f"no such run: {task_id}")
    if rec.get("status") not in {"running", "dispatching", "pausing", "pause_requested"}:
        return _error(
            f"run {task_id} is {rec.get('status')!r}; injections only land while the run is live"
        )
    target = write_injection(task_id, body)
    if not target:
        return _error("failed to write injection (disk?)")
    return (
        f"nightshift: staged injection for {task_id}\n"
        f"  file: {target}\n"
        f"  note: Week 2 has no core hook to push into a mid-run subagent — see\n"
        f"  docs/inject-limitation.md. The text is on disk and the transcript\n"
        f"  mirror shows `[INJECT pending]`; an operator can pipe it into the\n"
        f"  child via the existing TUI surface or the next subagent turn."
    )


# ---------------------------------------------------------------------------
# Subagent start/stop hooks — write the final state from the core's side
# ---------------------------------------------------------------------------

_HOOKS_INSTALLED = False
_HOOKS_LOCK = threading.Lock()


def install_lifecycle_hooks(ctx: Any) -> None:
    """Subscribe to `subagent_start` and `subagent_stop` to keep the run
    state in sync with the core's view of the child agent's lifecycle.

    The core emits these hooks once per child (not per dispatch), keyed
    on the subagent_id (= our delegation_id minus the `deleg_` prefix,
    but the core does not enforce naming — it just passes the kwargs).
    We match by delegation_id.

    Idempotency contract: re-calling on the SAME ctx is a no-op; calling
    on a FRESH ctx installs a fresh callback. Tests build a new
    FakeContext per scenario and expect each one to receive exactly one
    hook, so the per-ctx `_nightshift_hooks_attached` marker is what
    gates the install, not a module-level global.
    """
    with _HOOKS_LOCK:
        # Per-ctx marker — survives across module-level state changes
        # and lets tests build fresh contexts and still get a hook.
        if getattr(ctx, "_nightshift_hooks_attached", False):
            return
        try:
            ctx.register_hook("subagent_stop", _on_subagent_stop)
            setattr(ctx, "_nightshift_hooks_attached", True)
        except Exception as exc:
            logger.debug("nightshift lifecycle hook install failed: %s", exc)


def _on_subagent_stop(**kwargs: Any) -> None:
    """Map a `subagent_stop` event onto run state + transcript marker."""
    subagent_id = str(kwargs.get("subagent_id") or "")
    status = str(kwargs.get("status") or "completed")
    summary = kwargs.get("summary")
    exit_reason = kwargs.get("exit_reason")
    error = kwargs.get("error")
    # Find the run whose delegation_id matches the subagent_id.
    for rec in list_runs():
        if rec.get("delegation_id") == subagent_id:
            patch: Dict[str, Any] = {
                "status": status,
                "finished_at": _now_iso(),
            }
            if summary:
                patch["summary"] = str(summary)[:2000]
            if exit_reason:
                patch["exit_reason"] = str(exit_reason)
            if error:
                patch["error"] = str(error)[:500]
            save_state(rec["task_id"], patch)
            line = f"[{_hh_mm_ss()}] final | status={status}"
            if exit_reason:
                line += f" exit_reason={exit_reason}"
            if summary:
                line += f" summary={str(summary)[:200]}"
            append_transcript_line(rec["task_id"], line)
            break


# ---------------------------------------------------------------------------
# Core interop
# ---------------------------------------------------------------------------

def _interrupt_core_subagent(delegation_id: str) -> bool:
    """Call into the core to interrupt a single running subagent.

    Returns True if the core acknowledged the interrupt (i.e. found the
    subagent in its active registry). False is a soft failure: the run
    may already be finished, or the process may have restarted.
    """
    if not delegation_id:
        return False
    try:
        from tools.delegate_tool import interrupt_subagent
        return bool(interrupt_subagent(delegation_id))
    except Exception as exc:
        logger.debug("interrupt_subagent(%s) raised: %s", delegation_id, exc)
        return False


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    import time as _t
    return _t.strftime("%Y-%m-%dT%H:%M:%S")


def _hh_mm_ss() -> str:
    import time as _t
    return _t.strftime("%H:%M:%S")


def _safe_json(text: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(text, str):
        return None
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


# ---------------------------------------------------------------------------
# Public binding entry point (called by __init__.py:register)
# ---------------------------------------------------------------------------

def register_commands(ctx: Any) -> None:
    """Bind `ctx` and register all five slash commands. Idempotent."""
    _bind_plugin_context(ctx)
    install_lifecycle_hooks(ctx)

    # Use `partial` to capture ctx — the framework's dispatch site
    # (cli.py:_get_plugin_cmd_handler_names → plugin_handler(user_args))
    # calls handlers with a single `raw_args` argument and does not pass
    # the plugin context. Binding here is the only way the handlers can
    # reach back to ctx.dispatch_tool().
    ctx.register_command(
        name="nightshift",
        description="Dispatch a background delegated task and write run state.",
        handler=partial(handle_nightshift),
    )
    ctx.register_command(
        name="nightshift-status",
        description="List all nightshift runs, or show one run by id.",
        handler=partial(handle_nightshift_status),
    )
    ctx.register_command(
        name="nightshift-tail",
        description="Show the last N lines of a nightshift run transcript (default 40).",
        handler=partial(handle_nightshift_tail),
    )
    ctx.register_command(
        name="nightshift-pause",
        description="Request a single subagent to stop at its next iteration boundary.",
        handler=partial(handle_nightshift_pause),
    )
    ctx.register_command(
        name="nightshift-resume",
        description="Clear a run's pause flag (Week 2: marks reschedulable, not auto-rerun).",
        handler=partial(handle_nightshift_resume),
    )
    ctx.register_command(
        name="nightshift-inject",
        description='Stage a new instruction for a live subagent (Week 2: disk-staged).',
        handler=partial(handle_nightshift_inject),
    )


# ---------------------------------------------------------------------------
# Legacy Week 1 entry points (kept for the test file imports)
# ---------------------------------------------------------------------------

def handle_nightshift_status_legacy(ctx: Any, raw_args: str) -> str:
    """Week 1 command signature `def handle(ctx, raw_args)` — now unused
    because the framework dispatches with a single `raw_args` argument.
    Kept so old test imports keep working until the tests are ported."""
    return handle_nightshift_status(raw_args)
