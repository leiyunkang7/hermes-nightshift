"""Behavior tests for the Week 2 command seam.

These tests cover the parts of the Week 2 slice that do NOT require a
live parent agent / tool registry:

  - State store CRUD (state.json, transcript.md, injections/)
  - Mirror writes and tail reader
  - Command handler logic when a delegated `dispatch_tool` is faked
  - Pause / inject command surface
  - PluginContext-binding (register_commands binds ctx and registers
    all six slash commands; subsequent `partial(handle)` invocations
    pass a single `raw_args` string and reach the bound ctx)

The smoke test in `tests/test_week2_smoke.py` exercises the real
delegate_task path against the core; the rest of the suite stays
hermetic so the unit tests run without hermes on PYTHONPATH.

The repo does not have pytest wired up; we run tests as a flat script
(see README § Development). To stay compatible with that, tests that
need a per-test tmp dir accept it as a positional `tmp_path` argument
— the runner looks the name up at call time and passes
`tmp_path()` from this module, or pytest's fixture if invoked under
pytest.
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).parents[1]


def tmp_path():
    """Return a fresh temp dir for one test. The caller is expected to
    clean up; under pytest, prefer the fixture."""
    return Path(tempfile.mkdtemp(prefix="nightshift_test_"))


# Load the plugin modules the same way the framework does: against the
# `hermes_plugins.nightshift` namespace. That is the package name the
# loader uses when it `spec_from_file_location`s `__init__.py`; sibling
# files have to be pre-registered in sys.modules under that same parent
# (the framework does not auto-load siblings). We replicate that here.
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
    """Pre-load all submodules; returns (state, commands, init)."""
    # Load in dependency order: nightshift_state first (it has no
    # internal sibling imports), then nightshift_commands (which now
    # uses `import nightshift_state` — deferred form, not the broken
    # `from nightshift_state import ...` — so the module object just
    # needs to be in sys.modules).
    _load_submodule("nightshift_state")
    commands = _load_submodule("nightshift_commands")
    # Load __init__.py last so it can pre-register both submodules
    # in sys.modules. We do NOT call its `register()` here (no real
    # PluginContext); the test does that explicitly.
    spec = importlib.util.spec_from_file_location(
        _PKG, _PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(_PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    init = importlib.util.module_from_spec(spec)
    init.__package__ = _PKG
    init.__path__ = [str(_PLUGIN_DIR)]  # type: ignore[attr-defined]
    sys.modules[_PKG] = init
    spec.loader.exec_module(init)
    return sys.modules[f"{_PKG}.nightshift_state"], commands, init


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _run_all_tests():
    """Iterate the public test_* functions and execute each, supplying
    a fresh tmp_path when the signature asks for one. Returns a list of
    (name, error_str) failures."""
    failures = []
    test_names = sorted(x for x in globals() if x.startswith("test_"))
    for name in test_names:
        fn = globals()[name]
        try:
            sig = __import__("inspect").signature(fn)
            if "tmp_path" in sig.parameters:
                fn(tmp_path=tmp_path())
            else:
                fn()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            failures.append((name, str(exc)))
        else:
            print(f"PASS {name}")
    return failures


# ---------------------------------------------------------------------------
# Submodule bootstrap test (regression for the import error)
# ---------------------------------------------------------------------------

def test_submodule_bootstrap_loads_both_siblings():
    """The exact failure Week 1 hit in production: `from nightshift_commands
    import ...` resolved to a not-yet-registered name and exploded. After
    the Week 2 fix, pre-loading both files against the package parent lets
    those imports succeed."""
    _ensure_pkg_parent()
    # Pre-load state first (commands imports from state).
    state = _load_submodule("nightshift_state")
    commands = _load_submodule("nightshift_commands")
    assert hasattr(commands, "handle_nightshift")
    assert hasattr(state, "save_state")
    assert sys.modules[f"{_PKG}.nightshift_state"] is state
    assert sys.modules[f"{_PKG}.nightshift_commands"] is commands


def test_init_register_calls_ensure_submodule_for_both_siblings():
    """__init__.py:register() must pre-register nightshift_state and
    nightshift_commands into sys.modules under the package name. We can't
    call the real register() (no PluginContext), but we can verify the
    source contains the call sites and the helper function exists."""
    state, commands, init = _bootstrap()
    assert callable(init._ensure_submodule)
    source = (_PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    assert "_ensure_submodule" in source
    assert "nightshift_state" in source
    assert "nightshift_commands" in source
    assert "register_commands" in source


# ---------------------------------------------------------------------------
# State store tests
# ---------------------------------------------------------------------------

class _IsolatedHome:
    """Swap HERMES_HOME so state writes land in a temp dir, restore on exit."""

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


def test_save_and_load_state_round_trips(tmp_path):
    _bootstrap()
    with _IsolatedHome(tmp_path):
        new_task_id = sys.modules[_PKG + ".nightshift_state"].new_task_id
        save_state = sys.modules[_PKG + ".nightshift_state"].save_state
        load_state = sys.modules[_PKG + ".nightshift_state"].load_state
        tid = new_task_id()
        save_state(tid, {"status": "running", "goal": "do a thing", "started_at": "x"})
        rec = load_state(tid)
        assert rec is not None
        assert rec["status"] == "running"
        assert rec["goal"] == "do a thing"
        # save_state merges, not replaces
        save_state(tid, {"status": "pausing"})
        rec = load_state(tid)
        assert rec["status"] == "pausing"
        assert rec["goal"] == "do a thing"  # preserved
        # updated_at always advances
        assert "updated_at" in rec


def test_list_runs_newest_first(tmp_path):
    _bootstrap()
    with _IsolatedHome(tmp_path):
        new_task_id = sys.modules[_PKG + ".nightshift_state"].new_task_id
        save_state = sys.modules[_PKG + ".nightshift_state"].save_state
        list_runs = sys.modules[_PKG + ".nightshift_state"].list_runs
        a = new_task_id()
        b = new_task_id()
        save_state(a, {"status": "running", "started_at": "2026-01-01T00:00:00"})
        save_state(b, {"status": "running", "started_at": "2026-01-02T00:00:00"})
        runs = list_runs()
        ids = [r["task_id"] for r in runs]
        assert ids == [b, a]


def test_transcript_mirror_writes_header_and_appends(tmp_path):
    _bootstrap()
    with _IsolatedHome(tmp_path):
        new_task_id = sys.modules[_PKG + ".nightshift_state"].new_task_id
        write_transcript_header = sys.modules[_PKG + ".nightshift_state"].write_transcript_header
        append_transcript_line = sys.modules[_PKG + ".nightshift_state"].append_transcript_line
        transcript_path = sys.modules[_PKG + ".nightshift_state"].transcript_path
        last_transcript_lines = sys.modules[_PKG + ".nightshift_state"].last_transcript_lines
        tid = new_task_id()
        write_transcript_header(tid, "test goal", source_log="/tmp/source.log")
        append_transcript_line(tid, "12:00:00 user  | kickoff")
        append_transcript_line(tid, "12:00:01 final | done")
        body = transcript_path(tid).read_text(encoding="utf-8")
        assert "test goal" in body
        assert "/tmp/source.log" in body
        assert "12:00:00 user" in body
        assert "12:00:01 final" in body
        tail = last_transcript_lines(tid, n=10)
        assert "12:00:01 final" in tail


def test_write_injection_creates_file_and_marks_transcript(tmp_path):
    _bootstrap()
    with _IsolatedHome(tmp_path):
        new_task_id = sys.modules[_PKG + ".nightshift_state"].new_task_id
        write_injection = sys.modules[_PKG + ".nightshift_state"].write_injection
        write_transcript_header = sys.modules[_PKG + ".nightshift_state"].write_transcript_header
        transcript_path = sys.modules[_PKG + ".nightshift_state"].transcript_path
        injections_dir = sys.modules[_PKG + ".nightshift_state"].injections_dir
        tid = new_task_id()
        write_transcript_header(tid, "g", source_log=None)
        path = write_injection(tid, "stop, switch approach")
        assert path is not None
        target = Path(path)
        assert target.exists()
        assert "switch approach" in target.read_text(encoding="utf-8")
        # Transcripts gain an [INJECT pending] line
        body = transcript_path(tid).read_text(encoding="utf-8")
        assert "INJECT pending" in body
        # Empty text returns None
        assert write_injection(tid, "   \n  ") is None


# ---------------------------------------------------------------------------
# Command handler tests
# ---------------------------------------------------------------------------

class _FakeContext:
    """Mimics PluginContext for handler tests."""

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


def test_register_commands_registers_all_six():
    _bootstrap()
    state, commands, _ = _bootstrap()
    ctx = _FakeContext(_dispatched_payload())
    commands.register_commands(ctx)
    expected = {
        "nightshift", "nightshift-status", "nightshift-tail",
        "nightshift-pause", "nightshift-resume", "nightshift-inject",
    }
    assert set(ctx.commands.keys()) == expected
    # Bound ctx is reachable through the module
    assert commands._PLUGIN_CTX is ctx


def test_nightshift_dispatches_background_and_writes_state(tmp_path):
    _bootstrap()
    with _IsolatedHome(tmp_path):
        state, commands, _ = _bootstrap()
        ctx = _FakeContext(_dispatched_payload("deleg_zzzz0001"))
        commands.register_commands(ctx)
        # The framework calls `handler(user_args)` with a single arg.
        out = ctx.commands["nightshift"]["handler"]("inspect something")
        assert out.startswith("nightshift: dispatched"), out
        assert "deleg_zzzz0001" in out
        # dispatch_tool was called with background=True, role="leaf"
        assert ctx.calls, "dispatch_tool was not called"
        name, args, kwargs = ctx.calls[0]
        assert name == "delegate_task"
        assert args["goal"] == "inspect something"
        assert args["role"] == "leaf"
        assert args["background"] is True
        # State record was written
        list_runs = sys.modules[_PKG + ".nightshift_state"].list_runs
        runs = list_runs()
        assert len(runs) == 1
        rec = runs[0]
        assert rec["status"] == "running"
        assert rec["delegation_id"] == "deleg_zzzz0001"
        assert rec["goal"] == "inspect something"
        # Transcript mirror has a header that points at the source log
        transcript_path = sys.modules[_PKG + ".nightshift_state"].transcript_path
        body = transcript_path(rec["task_id"]).read_text(encoding="utf-8")
        assert "/cache/delegation/live/abc/task-0.log" in body


def test_nightshift_rejects_empty_goal():
    _bootstrap()
    state, commands, _ = _bootstrap()
    ctx = _FakeContext(_dispatched_payload())
    commands.register_commands(ctx)
    out = ctx.commands["nightshift"]["handler"]("   \n  ")
    assert "usage" in out
    assert ctx.calls == []


def test_nightshift_surfaces_dispatch_error(tmp_path):
    _bootstrap()
    with _IsolatedHome(tmp_path):
        state, commands, _ = _bootstrap()
        ctx = _FakeContext(json.dumps({"error": "delegation depth limit reached"}))
        commands.register_commands(ctx)
        out = ctx.commands["nightshift"]["handler"]("do work")
        assert "depth limit" in out
        list_runs = sys.modules[_PKG + ".nightshift_state"].list_runs
        runs = list_runs()
        assert len(runs) == 1
        assert runs[0]["status"] == "dispatch_error"


def test_status_lists_runs_and_shows_one(tmp_path):
    _bootstrap()
    with _IsolatedHome(tmp_path):
        state, commands, _ = _bootstrap()
        ctx = _FakeContext(_dispatched_payload())
        commands.register_commands(ctx)
        # Two dispatches on the same ctx (real users don't have two
        # PluginContexts, and re-binding the same one is the realistic
        # path — the second /nightshift should hit the already-registered
        # command).
        ctx.commands["nightshift"]["handler"]("goal one")
        ctx.commands["nightshift"]["handler"]("goal two")
        # Status (no args) lists both
        out = ctx.commands["nightshift-status"]["handler"]("")
        assert "2 run(s)" in out
        # Status (with id) shows one
        list_runs = sys.modules[_PKG + ".nightshift_state"].list_runs
        first_id = list_runs()[-1]["task_id"]
        out2 = ctx.commands["nightshift-status"]["handler"](first_id)
        assert first_id in out2


def test_tail_returns_last_n_lines(tmp_path):
    _bootstrap()
    with _IsolatedHome(tmp_path):
        state, commands, _ = _bootstrap()
        ctx = _FakeContext(_dispatched_payload())
        commands.register_commands(ctx)
        ctx.commands["nightshift"]["handler"]("goal")
        list_runs = sys.modules[_PKG + ".nightshift_state"].list_runs
        tid = list_runs()[0]["task_id"]
        # Append a few more lines
        append_transcript_line = sys.modules[_PKG + ".nightshift_state"].append_transcript_line
        for i in range(5):
            append_transcript_line(tid, f"line-{i}")
        out = ctx.commands["nightshift-tail"]["handler"](f"{tid} 2")
        # The last 2 lines
        assert "line-3" in out
        assert "line-4" in out


def test_pause_uses_core_interrupt_when_present(tmp_path):
    _bootstrap()
    with _IsolatedHome(tmp_path):
        state, commands, _ = _bootstrap()
        ctx = _FakeContext(_dispatched_payload("deleg_pause01"))
        commands.register_commands(ctx)
        ctx.commands["nightshift"]["handler"]("goal")
        list_runs = sys.modules[_PKG + ".nightshift_state"].list_runs
        tid = list_runs()[0]["task_id"]
        # Patch _interrupt_core_subagent to simulate the core's response.
        seen = []
        def fake_interrupt(delegation_id):
            seen.append(delegation_id)
            return True
        commands._interrupt_core_subagent = fake_interrupt
        out = ctx.commands["nightshift-pause"]["handler"](tid)
        assert "pause requested" in out
        assert "core hit: yes" in out
        assert seen == ["deleg_pause01"]
        load_state = sys.modules[_PKG + ".nightshift_state"].load_state
        rec = load_state(tid)
        assert rec["status"] == "pausing"


def test_pause_marks_requested_when_core_has_no_active_record(tmp_path):
    _bootstrap()
    with _IsolatedHome(tmp_path):
        state, commands, _ = _bootstrap()
        ctx = _FakeContext(_dispatched_payload("deleg_unknown01"))
        commands.register_commands(ctx)
        ctx.commands["nightshift"]["handler"]("goal")
        list_runs = sys.modules[_PKG + ".nightshift_state"].list_runs
        tid = list_runs()[0]["task_id"]
        commands._interrupt_core_subagent = lambda delegation_id: False
        out = ctx.commands["nightshift-pause"]["handler"](tid)
        assert "core hit: no" in out
        load_state = sys.modules[_PKG + ".nightshift_state"].load_state
        rec = load_state(tid)
        assert rec["status"] == "pause_requested"


def test_pause_rejects_missing_run():
    _bootstrap()
    state, commands, _ = _bootstrap()
    ctx = _FakeContext(_dispatched_payload())
    commands.register_commands(ctx)
    out = ctx.commands["nightshift-pause"]["handler"]("ns_doesnotexist")
    assert "no such run" in out


def test_resume_only_for_paused_runs(tmp_path):
    _bootstrap()
    with _IsolatedHome(tmp_path):
        state, commands, _ = _bootstrap()
        ctx = _FakeContext(_dispatched_payload("deleg_resume01"))
        commands.register_commands(ctx)
        ctx.commands["nightshift"]["handler"]("goal")
        list_runs = sys.modules[_PKG + ".nightshift_state"].list_runs
        load_state = sys.modules[_PKG + ".nightshift_state"].load_state
        tid = list_runs()[0]["task_id"]
        # Resuming a running run should be rejected
        out = ctx.commands["nightshift-resume"]["handler"](tid)
        assert "for paused/interrupted runs only" in out
        # Mark as pausing, then resume dispatches a new run under <tid>-1.
        # Week 3 contract (issue #8): resume does NOT mutate the parent
        # status — it dispatches a fresh child and links via `resumed_from`.
        save_state = sys.modules[_PKG + ".nightshift_state"].save_state
        save_state(tid, {"status": "pausing"})
        out = ctx.commands["nightshift-resume"]["handler"](tid)
        assert f"{tid}-1" in out
        assert "resumed" in out
        # Parent state.json is preserved (audit trail)
        parent_rec = load_state(tid)
        assert parent_rec["status"] == "pausing"
        # Child state.json carries the link
        child_rec = load_state(f"{tid}-1")
        assert child_rec["resumed_from"] == tid


def test_inject_stages_text_and_marks_transcript(tmp_path):
    _bootstrap()
    with _IsolatedHome(tmp_path):
        state, commands, _ = _bootstrap()
        ctx = _FakeContext(_dispatched_payload("deleg_inject01"))
        commands.register_commands(ctx)
        ctx.commands["nightshift"]["handler"]("goal")
        list_runs = sys.modules[_PKG + ".nightshift_state"].list_runs
        tid = list_runs()[0]["task_id"]
        out = ctx.commands["nightshift-inject"]["handler"](f'{tid} "switch approach"')
        assert "staged injection" in out
        # The injections dir has one file
        injections_dir = sys.modules[_PKG + ".nightshift_state"].injections_dir
        transcript_path = sys.modules[_PKG + ".nightshift_state"].transcript_path
        files = list(injections_dir(tid).iterdir())
        assert len(files) == 1
        assert "switch approach" in files[0].read_text(encoding="utf-8")
        # Transcript mirror has the [INJECT pending] marker
        body = transcript_path(tid).read_text(encoding="utf-8")
        assert "INJECT pending" in body


def test_inject_rejects_when_run_not_live(tmp_path):
    _bootstrap()
    with _IsolatedHome(tmp_path):
        state, commands, _ = _bootstrap()
        ctx = _FakeContext(_dispatched_payload())
        commands.register_commands(ctx)
        ctx.commands["nightshift"]["handler"]("goal")
        list_runs = sys.modules[_PKG + ".nightshift_state"].list_runs
        save_state = sys.modules[_PKG + ".nightshift_state"].save_state
        tid = list_runs()[0]["task_id"]
        save_state(tid, {"status": "completed"})
        out = ctx.commands["nightshift-inject"]["handler"](f'{tid} "x"')
        assert "only land while the run is live" in out


def test_subagent_stop_hook_updates_run_state(tmp_path):
    _bootstrap()
    with _IsolatedHome(tmp_path):
        state, commands, _ = _bootstrap()
        ctx = _FakeContext(_dispatched_payload("deleg_hook01"))
        commands.register_commands(ctx)
        ctx.commands["nightshift"]["handler"]("goal")
        list_runs = sys.modules[_PKG + ".nightshift_state"].list_runs
        load_state = sys.modules[_PKG + ".nightshift_state"].load_state
        transcript_path = sys.modules[_PKG + ".nightshift_state"].transcript_path
        tid = list_runs()[0]["task_id"]
        # Fire the hook with a matching delegation_id
        for cb in ctx.hooks.get("subagent_stop", []):
            cb(subagent_id="deleg_hook01", status="completed", summary="did the work")
        rec = load_state(tid)
        assert rec["status"] == "completed"
        assert rec["summary"] == "did the work"
        # Transcript mirror gained a final marker
        body = transcript_path(tid).read_text(encoding="utf-8")
        assert "status=completed" in body
        assert "did the work" in body


def test_subagent_stop_hook_ignores_unknown_subagent_id(tmp_path):
    _bootstrap()
    with _IsolatedHome(tmp_path):
        state, commands, _ = _bootstrap()
        ctx = _FakeContext(_dispatched_payload("deleg_known01"))
        commands.register_commands(ctx)
        ctx.commands["nightshift"]["handler"]("goal")
        list_runs = sys.modules[_PKG + ".nightshift_state"].list_runs
        load_state = sys.modules[_PKG + ".nightshift_state"].load_state
        tid = list_runs()[0]["task_id"]
        before = load_state(tid)
        for cb in ctx.hooks.get("subagent_stop", []):
            cb(subagent_id="deleg_somebodyelse", status="completed")
        after = load_state(tid)
        assert after["status"] == before["status"]


def test_register_commands_is_idempotent():
    """Calling register twice on the same ctx must NOT double-register
    the same hook. The module-level _HOOKS_INSTALLED guard plus the
    per-ctx name check enforce this."""
    state, commands, _ = _bootstrap()
    # Reset the module-level flag so we get a clean test.
    commands._HOOKS_INSTALLED = False
    ctx = _FakeContext(_dispatched_payload())
    commands.register_commands(ctx)
    commands.register_commands(ctx)
    assert len(ctx.hooks.get("subagent_stop", [])) == 1
    assert set(ctx.commands.keys()) == {
        "nightshift", "nightshift-status", "nightshift-tail",
        "nightshift-pause", "nightshift-resume", "nightshift-inject",
    }
