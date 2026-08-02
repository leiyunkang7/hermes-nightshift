"""Behavior tests for the Week 3 resume-dispatch seam.

These tests cover the parts of the Week 3 slice that build on top of
Week 2's state store + command handlers:

  - Generation-counter uniqueness (root → -1, -2, …; chained → -N-1, …)
  - Missing / empty prior-transcript fallback (do not crash dispatch)
  - /nightshift-resume dispatch branch:
      * mints a new task_id per the counter rule
      * copies prior transcript into the new run dir
      * sets `resumed_from` in the new run's state.json
      * appends a marker to the parent's transcript
      * preserves the parent's state.json byte-for-byte (modulo timestamp)
      * passes the prior transcript in the new delegate_task's `context=`
      * rejects resumes from a non-paused status
  - The counter is recoverable from disk (no in-memory cache required)

These tests follow the Week 2 style: a `_FakeContext` mimics
PluginContext's dispatch + command registry, `_IsolatedHome` swaps
HERMES_HOME into a tmp dir, and `_dispatched_payload()` returns a
canned core response. Reuse the helpers from test_week2.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

# Same root + parent-package convention as test_week2.py.
ROOT = Path(__file__).parents[1]
_PKG = "hermes_plugins.nightshift"
_PARENT = "hermes_plugins"
_PLUGIN_DIR = ROOT


def _ensure_pkg_parent() -> None:
    if _PARENT not in sys.modules:
        import types
        ns = types.ModuleType(_PARENT)
        ns.__path__ = []  # type: ignore[attr-defined]
        sys.modules[_PARENT] = ns
    if _PKG not in sys.modules:
        import types
        m = types.ModuleType(_PKG)
        m.__package__ = _PKG
        m.__path__ = [str(_PLUGIN_DIR)]  # type: ignore[attr-defined]
        sys.modules[_PKG] = m


def _load_submodule(name: str):
    _ensure_pkg_parent()
    full = f"{_PKG}.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(
        full, _PLUGIN_DIR / f"{name}.py",
        submodule_search_locations=[str(_PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    m.__package__ = _PKG
    m.__path__ = [str(_PLUGIN_DIR)]  # type: ignore[attr-defined]
    sys.modules[full] = m
    spec.loader.exec_module(m)
    return m


def _bootstrap():
    """Pre-load submodules; returns (state, commands)."""
    _load_submodule("nightshift_state")
    commands = _load_submodule("nightshift_commands")
    return sys.modules[f"{_PKG}.nightshift_state"], commands


def _run_all_tests():
    failures = []
    test_names = sorted(x for x in globals() if x.startswith("test_"))
    for name in test_names:
        fn = globals()[name]
        try:
            import inspect
            sig = inspect.signature(fn)
            if "tmp_path" in sig.parameters:
                fn(tmp_path=Path(tempfile.mkdtemp(prefix="nightshift_w3_")))
            else:
                fn()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            failures.append((name, str(exc)))
        else:
            print(f"PASS {name}")
    return failures


class _IsolatedHome:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self._saved = None

    def __enter__(self):
        self._saved = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.tmp)
        return self

    def __exit__(self, *exc):
        if self._saved is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._saved


class _FakeContext:
    def __init__(self, response: str):
        self._response = response
        self.calls = []
        self.commands = {}
        self.hooks = {}

    def dispatch_tool(self, name, args, **kwargs):
        self.calls.append((name, dict(args), kwargs))
        return self._response

    def register_command(self, *, name, description="", handler=None, **_):
        self.commands[name] = {"description": description, "handler": handler}

    def register_hook(self, event, callback):
        self.hooks.setdefault(event, []).append(callback)


def _dispatched_payload(delegation_id="deleg_abc12345") -> str:
    return json.dumps({
        "status": "dispatched",
        "mode": "background",
        "count": 1,
        "delegation_id": delegation_id,
        "goals": ["test"],
        "live_transcripts": ["/cache/delegation/live/abc/task-0.log"],
    })


# ---------------------------------------------------------------------------
# Counter uniqueness (pure disk, no in-memory state)
# ---------------------------------------------------------------------------

def test_next_child_task_id_returns_dash_one_for_root(tmp_path):
    _bootstrap()
    state, _ = _bootstrap()
    next_child_task_id = getattr(state, "next_child_task_id", None)
    assert next_child_task_id is not None, "next_child_task_id() missing from nightshift_state"
    with _IsolatedHome(tmp_path):
        # With no runs on disk, root → "ns_xxx-1"
        assert next_child_task_id("ns_root") == "ns_root-1"


def test_next_child_task_id_returns_dash_one_when_chain_empty(tmp_path):
    """Resuming a never-resumed task where a previous chain marker exists
    under a different parent must NOT confuse the counter."""
    _bootstrap()
    state, _ = _bootstrap()
    next_child_task_id = state.next_child_task_id
    save_state = state.save_state
    with _IsolatedHome(tmp_path):
        # Simulate an unrelated resume chain for ns_other
        save_state("ns_other-1", {"status": "running", "task_id": "ns_other-1"})
        # Fresh resume of ns_root must still pick 1
        assert next_child_task_id("ns_root") == "ns_root-1"


def test_next_child_task_id_picks_next_free_integer(tmp_path):
    """When ns_root-1 and ns_root-2 exist, the next must be -3, not -1."""
    _bootstrap()
    state, _ = _bootstrap()
    next_child_task_id = state.next_child_task_id
    save_state = state.save_state
    with _IsolatedHome(tmp_path):
        save_state("ns_root-1", {"status": "running"})
        save_state("ns_root-2", {"status": "running"})
        assert next_child_task_id("ns_root") == "ns_root-3"


def test_next_child_task_id_handles_gap_in_counter(tmp_path):
    """Gap (ns_root-1, ns_root-3 exist) — pick -2, the lowest free."""
    _bootstrap()
    state, _ = _bootstrap()
    next_child_task_id = state.next_child_task_id
    save_state = state.save_state
    with _IsolatedHome(tmp_path):
        save_state("ns_root-1", {"status": "running"})
        save_state("ns_root-3", {"status": "running"})
        assert next_child_task_id("ns_root") == "ns_root-2"


def test_next_child_task_id_chains_under_parent(tmp_path):
    """Resuming ns_root-1 must produce ns_root-1-1, not ns_root-2."""
    _bootstrap()
    state, _ = _bootstrap()
    next_child_task_id = state.next_child_task_id
    save_state = state.save_state
    with _IsolatedHome(tmp_path):
        save_state("ns_root-1", {"status": "running"})
        assert next_child_task_id("ns_root-1") == "ns_root-1-1"


def test_next_child_task_id_recognizes_chained_children(tmp_path):
    """ns_root-1-2 exists; the next under ns_root-1 must be -3."""
    _bootstrap()
    state, _ = _bootstrap()
    next_child_task_id = state.next_child_task_id
    save_state = state.save_state
    with _IsolatedHome(tmp_path):
        save_state("ns_root-1-1", {"status": "running"})
        save_state("ns_root-1-2", {"status": "running"})
        assert next_child_task_id("ns_root-1") == "ns_root-1-3"


def test_next_child_task_id_does_not_confuse_chains_with_root(tmp_path):
    """ns_root-1 and ns_root-1-1 exist; next under ns_root must be -2
    (not -1-2 or anything weird)."""
    _bootstrap()
    state, _ = _bootstrap()
    next_child_task_id = state.next_child_task_id
    save_state = state.save_state
    with _IsolatedHome(tmp_path):
        save_state("ns_root-1", {"status": "running"})
        save_state("ns_root-1-1", {"status": "running"})
        assert next_child_task_id("ns_root") == "ns_root-2"


# ---------------------------------------------------------------------------
# run_is_resumable + prior-transcript copy helper
# ---------------------------------------------------------------------------

def test_prior_transcript_copy_creates_prior_transcript_md():
    """copy_prior_transcript(parent, child) writes a copy under the new run dir."""
    _bootstrap()
    state, _ = _bootstrap()
    save_state = state.save_state
    write_transcript_header = state.write_transcript_header
    append_transcript_line = state.append_transcript_line
    run_dir = state.run_dir
    transcript_path = state.transcript_path
    parent = "ns_p"
    write_transcript_header(parent, "the goal", source_log=None)
    append_transcript_line(parent, "line one\n")
    append_transcript_line(parent, "line two\n")
    # Save state so run_dir() can find parent in list_runs()
    save_state(parent, {"status": "running", "task_id": parent})
    child = "ns_p-1"
    fn = getattr(state, "copy_prior_transcript", None)
    assert fn is not None, "copy_prior_transcript() missing"
    fn(parent, child)
    target = run_dir(child) / "prior_transcript.md"
    assert target.exists(), "prior_transcript.md not written under new run dir"
    assert target.read_text(encoding="utf-8") == transcript_path(parent).read_text(encoding="utf-8")


def test_prior_transcript_copy_writes_fallback_marker_when_parent_missing():
    """If the parent's transcript.md is missing, write a one-line marker instead."""
    _bootstrap()
    state, _ = _bootstrap()
    save_state = state.save_state
    run_dir = state.run_dir
    parent = "ns_q"
    save_state(parent, {"status": "running", "task_id": parent})  # no transcript.md
    child = "ns_q-1"
    fn = state.copy_prior_transcript
    fn(parent, child)
    target = run_dir(child) / "prior_transcript.md"
    assert target.exists()
    body = target.read_text(encoding="utf-8")
    assert "prior transcript unavailable" in body


def test_prior_transcript_copy_writes_fallback_marker_when_empty(tmp_path):
    """Empty transcript.md (zero-byte file) also gets the fallback marker."""
    _bootstrap()
    state, _ = _bootstrap()
    save_state = state.save_state
    write_transcript_header = state.write_transcript_header
    run_dir = state.run_dir
    with _IsolatedHome(tmp_path):
        parent = "ns_e"
        write_transcript_header(parent, "g", source_log=None)
        # Overwrite with empty
        (run_dir(parent) / "transcript.md").write_text("", encoding="utf-8")
        save_state(parent, {"status": "running", "task_id": parent})
        child = "ns_e-1"
        state.copy_prior_transcript(parent, child)
        target = run_dir(child) / "prior_transcript.md"
        body = target.read_text(encoding="utf-8")
        assert "prior transcript unavailable" in body


# ---------------------------------------------------------------------------
# /nightshift-resume dispatch branch
# ---------------------------------------------------------------------------

def _setup_paused_run(state, commands, ctx, *, goal="do the thing", delegation_id="deleg_resume01"):
    """Dispatch /nightshift "goal" then mark the run as paused."""
    commands.register_commands(ctx)
    ctx.commands["nightshift"]["handler"](goal)
    tid = state.list_runs()[0]["task_id"]
    state.save_state(tid, {"status": "pause_requested"})
    return tid


def test_resume_rejects_when_run_is_running(tmp_path):
    """The status guard is preserved: resume of a `running` run rejects."""
    _bootstrap()
    state, commands, _ = _bootstrap(), None, None  # type: ignore[misc]
    state, commands = _bootstrap()
    with _IsolatedHome(tmp_path):
        ctx = _FakeContext(_dispatched_payload("deleg_x"))
        tid = _setup_paused_run(state, commands, ctx)
        state.save_state(tid, {"status": "running"})
        out = ctx.commands["nightshift-resume"]["handler"](tid)
        assert "for paused/interrupted runs only" in out
        # No state.json for the rejected resume was created
        assert not (state.state_path(f"{tid}-1")).exists()


def test_resume_dispatches_with_full_transcript_in_context(tmp_path):
    """The dispatched delegate_task's `context=` equals the parent's transcript.md."""
    _bootstrap()
    state, commands = _bootstrap()
    with _IsolatedHome(tmp_path):
        ctx = _FakeContext(_dispatched_payload("deleg_resume02"))
        tid = _setup_paused_run(state, commands, ctx, goal="research topic X")
        # Write a known transcript body so we can verify it landed in context
        state.append_transcript_line(tid, "## user\nplease research X\n")
        state.append_transcript_line(tid, "## assistant\nfirst finding: Y\n")
        ctx.commands["nightshift-resume"]["handler"](tid)
        # Find the dispatch call to delegate_task and inspect `context=`
        dispatch_calls = [c for c in ctx.calls if c[0] == "delegate_task"]
        assert len(dispatch_calls) == 2, f"expected 2 dispatches (orig + resume); got {len(dispatch_calls)}"
        resume_args = dispatch_calls[-1][1]
        assert resume_args["goal"] == "research topic X"
        assert "context" in resume_args
        assert "first finding: Y" in resume_args["context"]
        assert "please research X" in resume_args["context"]


def test_resume_mints_first_generation_counter(tmp_path):
    _bootstrap()
    state, commands = _bootstrap()
    with _IsolatedHome(tmp_path):
        ctx = _FakeContext(_dispatched_payload("deleg_resume03"))
        tid = _setup_paused_run(state, commands, ctx)
        out = ctx.commands["nightshift-resume"]["handler"](tid)
        assert f"{tid}-1" in out
        # A new run dir exists for ns_xxx-1
        assert state.run_dir(f"{tid}-1").exists()


def test_resume_chains_generation_counter(tmp_path):
    """Two resumes from the same root produce -1, then -2."""
    _bootstrap()
    state, commands = _bootstrap()
    with _IsolatedHome(tmp_path):
        ctx = _FakeContext(_dispatched_payload("deleg_resume04"))
        tid = _setup_paused_run(state, commands, ctx)
        # First resume — flattens state to allow a second resume
        ctx.commands["nightshift-resume"]["handler"](tid)
        state.save_state(tid, {"status": "pause_requested"})
        ctx.commands["nightshift-resume"]["handler"](tid)
        ids = sorted(r["task_id"] for r in state.list_runs())
        assert f"{tid}-1" in ids
        assert f"{tid}-2" in ids


def test_resume_grandchild_uses_chained_counter(tmp_path):
    """Resuming a resumed run uses <root>-1-1, not <root>-2."""
    state, commands = _bootstrap()
    with _IsolatedHome(tmp_path):
        ctx = _FakeContext(_dispatched_payload("deleg_resume05"))
        tid = _setup_paused_run(state, commands, ctx)
        ctx.commands["nightshift-resume"]["handler"](tid)
        # Now resume ns_root-1
        child = f"{tid}-1"
        state.save_state(child, {"status": "pause_requested"})
        ctx.commands["nightshift-resume"]["handler"](child)
        ids = sorted(r["task_id"] for r in state.list_runs())
        assert f"{tid}-1-1" in ids


def test_resume_writes_resumed_from_to_state_json(tmp_path):
    _bootstrap()
    state, commands = _bootstrap()
    with _IsolatedHome(tmp_path):
        ctx = _FakeContext(_dispatched_payload("deleg_resume06"))
        tid = _setup_paused_run(state, commands, ctx)
        ctx.commands["nightshift-resume"]["handler"](tid)
        rec = state.load_state(f"{tid}-1")
        assert rec is not None
        assert rec.get("resumed_from") == tid


def test_resume_appends_marker_to_parent_transcript(tmp_path):
    _bootstrap()
    state, commands = _bootstrap()
    with _IsolatedHome(tmp_path):
        ctx = _FakeContext(_dispatched_payload("deleg_resume07"))
        tid = _setup_paused_run(state, commands, ctx)
        ctx.commands["nightshift-resume"]["handler"](tid)
        body = state.transcript_path(tid).read_text(encoding="utf-8")
        assert f"resume: dispatched {tid}-1" in body
        assert "prior transcript" in body


def test_resume_preserves_parent_state_json(tmp_path):
    """The parent's state.json must NOT be mutated by resume (audit trail)."""
    _bootstrap()
    state, commands = _bootstrap()
    with _IsolatedHome(tmp_path):
        ctx = _FakeContext(_dispatched_payload("deleg_resume08"))
        tid = _setup_paused_run(state, commands, ctx)
        before = state.state_path(tid).read_text(encoding="utf-8")
        ctx.commands["nightshift-resume"]["handler"](tid)
        after = state.state_path(tid).read_text(encoding="utf-8")
        assert before == after, "parent state.json must be byte-identical after resume"


def test_resume_copies_prior_transcript_to_new_run_dir(tmp_path):
    _bootstrap()
    state, commands = _bootstrap()
    with _IsolatedHome(tmp_path):
        ctx = _FakeContext(_dispatched_payload("deleg_resume09"))
        tid = _setup_paused_run(state, commands, ctx, goal="g")
        state.append_transcript_line(tid, "meaningful transcript line\n")
        ctx.commands["nightshift-resume"]["handler"](tid)
        child = f"{tid}-1"
        prior = state.run_dir(child) / "prior_transcript.md"
        assert prior.exists()
        assert "meaningful transcript line" in prior.read_text(encoding="utf-8")


def test_resume_handles_missing_parent_transcript(tmp_path):
    """No transcript.md on the parent: dispatch still succeeds with the fallback marker."""
    _bootstrap()
    state, commands = _bootstrap()
    with _IsolatedHome(tmp_path):
        ctx = _FakeContext(_dispatched_payload("deleg_resume10"))
        tid = _setup_paused_run(state, commands, ctx)
        # Truncate the parent's transcript.md to empty
        state.transcript_path(tid).write_text("", encoding="utf-8")
        out = ctx.commands["nightshift-resume"]["handler"](tid)
        assert "resumed" in out
        child = f"{tid}-1"
        prior = state.run_dir(child) / "prior_transcript.md"
        assert prior.exists()
        body = prior.read_text(encoding="utf-8")
        assert "prior transcript unavailable" in body


def test_resume_returns_user_facing_message_with_new_and_parent_ids(tmp_path):
    _bootstrap()
    state, commands = _bootstrap()
    with _IsolatedHome(tmp_path):
        ctx = _FakeContext(_dispatched_payload("deleg_resume11"))
        tid = _setup_paused_run(state, commands, ctx)
        out = ctx.commands["nightshift-resume"]["handler"](tid)
        # New id, parent id, tail/pause/inject reminders all present
        assert tid in out
        assert f"{tid}-1" in out
        assert f"tail with: /nightshift-tail {tid}-1" in out
        assert f"pause with: /nightshift-pause {tid}-1" in out


# ---------------------------------------------------------------------------
# Spec-mandated regression tests (issue #8 §Acceptance)
# ---------------------------------------------------------------------------

def test_old_run_is_still_readable_after_resume(tmp_path):
    """Spec: 'Two regression tests: (a) old run is still readable after resume.'

    After resume, the parent's run dir, state.json, and transcript.md must
    all still be present and loadable through the public state API.
    """
    _bootstrap()
    state, commands = _bootstrap()
    with _IsolatedHome(tmp_path):
        ctx = _FakeContext(_dispatched_payload("deleg_reg01"))
        tid = _setup_paused_run(state, commands, ctx, goal="original goal text")
        ctx.commands["nightshift-resume"]["handler"](tid)
        # Parent dir + state.json + transcript.md all still exist
        assert state.run_dir(tid).exists()
        assert state.state_path(tid).exists()
        assert state.transcript_path(tid).exists()
        # Parent's state is loadable and still has the original goal
        rec = state.load_state(tid)
        assert rec is not None
        assert rec["goal"] == "original goal text"
        # list_runs surfaces both
        ids = {r["task_id"] for r in state.list_runs()}
        assert tid in ids
        assert f"{tid}-1" in ids


def test_new_run_goal_matches_parent_goal(tmp_path):
    """Spec: '(b) the new run's `goal` matches the old run's `goal`.'"""
    _bootstrap()
    state, commands = _bootstrap()
    with _IsolatedHome(tmp_path):
        ctx = _FakeContext(_dispatched_payload("deleg_reg02"))
        tid = _setup_paused_run(state, commands, ctx, goal="exact goal the operator typed")
        ctx.commands["nightshift-resume"]["handler"](tid)
        new_rec = state.load_state(f"{tid}-1")
        assert new_rec is not None
        assert new_rec["goal"] == "exact goal the operator typed"
        parent_rec = state.load_state(tid)
        assert new_rec["goal"] == parent_rec["goal"]


if __name__ == "__main__":
    failures = _run_all_tests()
    print(f"TOTAL_FAILURES: {len(failures)}")
    for n, e in failures:
        print(f"FAIL {n}: {e[:200]}")
    sys.exit(0 if not failures else 1)