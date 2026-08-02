"""Persistent state for `nightshift` runs.

The Week 2 slice lives here. Each `/nightshift "<goal>"` call creates a
*run record* under `~/.hermes/nightshift/runs/<task_id>/`:

  - `state.json`   – machine-readable status (goal, status, started_at, …)
  - `transcript.md` – human-readable, append-only mirror of the live
    transcript the core writes at
    `cache/delegation/live/<delegation_id>/task-<n>.log`, with a header
    that names the task_id so users can `tail -f` from either side
  - `injections/`  – one file per `/nightshift-inject <id> "text"` call
                     (Week 2 best-effort: see docs/inject-limitation.md)

This module is the single source of truth for run state. Commands
(`/nightshift`, `/nightshift-status`, `/nightshift-pause`, …) read and
write here only; they never reach into the core `delegate_task` to learn
about their own runs.

Design constraints (mirrored from `tools/delegation_live_log.py`):

* **Never raise into the slash command handler.** Every disk write is
  wrapped; the first failure logs and degrades to a debug message, so
  a misbehaving filesystem cannot break a user-facing command.
* **No long-lived file handles.** Appends reopen the file per write —
  crash-safe and immune to FD leaks.
* **No external state.** Task ids are stable across process restarts
  (the core `delegation_id` is the key). A process restart does not
  lose the ability to introspect or interrupt a running subagent —
  it just loses the in-memory active-subagent registry, which the
  core itself rebuilds from the same delegation_id.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Keep N most recent runs; older runs are pruned opportunistically.
MAX_RUNS_RETAINED = 50


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _hermes_home() -> Path:
    """Return the active Hermes home, profile-aware (matches the core's lookup)."""
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def nightshift_root() -> Path:
    """Root directory for all nightshift run state."""
    p = _hermes_home() / "nightshift"
    p.mkdir(parents=True, exist_ok=True)
    return p


def run_dir(task_id: str) -> Path:
    d = nightshift_root() / "runs" / task_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "injections").mkdir(exist_ok=True)
    return d


def state_path(task_id: str) -> Path:
    return run_dir(task_id) / "state.json"


def transcript_path(task_id: str) -> Path:
    return run_dir(task_id) / "transcript.md"


def injections_dir(task_id: str) -> Path:
    return run_dir(task_id) / "injections"


# ---------------------------------------------------------------------------
# Run ids
# ---------------------------------------------------------------------------

def new_task_id() -> str:
    """Generate a short, human-friendly run id. Same shape as the core's
    `delegation_id` so paths and ids round-trip cleanly through state."""
    return f"ns_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# State read / write
# ---------------------------------------------------------------------------

def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    """Write JSON atomically: write to .tmp then rename. Crash-safe."""
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:
        logger.debug("nightshift state write failed (%s): %s", path, exc)


def save_state(task_id: str, patch: Dict[str, Any]) -> None:
    """Merge `patch` into the existing state record and persist it.

    Existing keys not in `patch` are preserved. First write creates the
    record. Best-effort: a failure logs at DEBUG and returns — never raises.
    """
    try:
        path = state_path(task_id)
        if path.exists():
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {}
        else:
            state = {}
        state.update(patch)
        state.setdefault("task_id", task_id)
        state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _atomic_write_json(path, state)
    except Exception as exc:
        logger.debug("nightshift save_state(%s) failed: %s", task_id, exc)


def load_state(task_id: str) -> Optional[Dict[str, Any]]:
    """Return the current state for `task_id`, or None if it does not exist."""
    path = state_path(task_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("nightshift load_state(%s) failed: %s", task_id, exc)
        return None


def list_runs() -> List[Dict[str, Any]]:
    """Snapshot of all run records, newest first.

    Each entry is the state dict with `task_id` guaranteed. Records
    that fail to parse are skipped (not raised).
    """
    runs_dir = nightshift_root() / "runs"
    if not runs_dir.is_dir():
        return []
    records: List[Dict[str, Any]] = []
    for child in runs_dir.iterdir():
        if not child.is_dir():
            continue
        sp = child / "state.json"
        if not sp.exists():
            continue
        try:
            rec = json.loads(sp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rec.setdefault("task_id", child.name)
        records.append(rec)
    records.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return records


def prune_stale_runs(max_keep: int = MAX_RUNS_RETAINED) -> int:
    """Delete the oldest runs beyond `max_keep`. Returns count removed."""
    runs_dir = nightshift_root() / "runs"
    if not runs_dir.is_dir():
        return 0
    try:
        records = list_runs()
    except Exception:
        return 0
    if len(records) <= max_keep:
        return 0
    for rec in records[max_keep:]:
        tid = rec.get("task_id")
        if not tid:
            continue
        try:
            shutil.rmtree(runs_dir / tid, ignore_errors=True)
        except OSError:
            continue
    return len(records) - max_keep


# ---------------------------------------------------------------------------
# Resume chain — generation counter
# ---------------------------------------------------------------------------

def _direct_child_suffixes(parent_task_id: str) -> List[int]:
    """Return the integer suffixes of all direct children of `parent_task_id`.

    A "direct child" is any run whose task_id starts with `<parent>-` and
    whose suffix begins with a single integer (e.g. `ns_root-1`, `ns_root-2`,
    but NOT `ns_root-1-1` — that is a grandchild of `ns_root`). The chain
    rule: a parent owns integer counter space `1, 2, 3, …`; a child
    `ns_root-N` owns its own integer counter space prefixed with `-N-`.

    Pure disk read: scans `~/.hermes/nightshift/runs/` once. No in-memory
    cache — recovering from disk is the source of truth (the parent run
    may have been pruned between dispatches).
    """
    runs_dir = nightshift_root() / "runs"
    if not runs_dir.is_dir():
        return []
    out: List[int] = []
    prefix = f"{parent_task_id}-"
    for child in runs_dir.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if not name.startswith(prefix):
            continue
        # Suffix is everything after `parent-`. A direct child has a single
        # integer segment; chained children have `N-M` and must be ignored
        # at this level (their `N` is the parent's, not ours to claim).
        suffix = name[len(prefix):]
        if "-" in suffix:
            continue  # grandchild — skip; we only own the first integer segment
        try:
            out.append(int(suffix))
        except ValueError:
            continue
    return out


def next_child_task_id(parent_task_id: str) -> str:
    """Return the next free generation-counter child id for `parent_task_id`.

    Counter rule (locked in issue #7):

      root `ns_a1b2`             → children `ns_a1b2-1`, `ns_a1b2-2`, …
      child `ns_a1b2-1`          → grandchildren `ns_a1b2-1-1`, `ns_a1b2-1-2`, …
      resume picks the LOWEST free integer (handles gaps from prior prunes)

    Pure function: scans `runs/` once. No side effects, no in-memory state.
    """
    taken = set(_direct_child_suffixes(parent_task_id))
    n = 1
    while n in taken:
        n += 1
    return f"{parent_task_id}-{n}"


def copy_prior_transcript(parent_task_id: str, child_task_id: str) -> Optional[str]:
    """Copy the parent's `transcript.md` into the new run dir as `prior_transcript.md`.

    The copy is intentionally a copy (not a symlink) so the child run stays
    readable even if `prune_stale_runs` later removes the parent. If the
    parent's transcript is missing or empty, writes a one-line fallback
    marker instead — never raises. Returns the absolute path of the file
    written, or None on a hard failure (permissions, etc.).
    """
    try:
        src = transcript_path(parent_task_id)
        dst = run_dir(child_task_id) / "prior_transcript.md"
        if src.exists() and src.stat().st_size > 0:
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            dst.write_text(
                "# prior transcript unavailable\n"
                f"# (parent run {parent_task_id} had no transcript.md on disk)\n",
                encoding="utf-8",
            )
        return str(dst)
    except Exception as exc:
        logger.debug(
            "nightshift copy_prior_transcript(%s -> %s) failed: %s",
            parent_task_id, child_task_id, exc,
        )
        return None


# ---------------------------------------------------------------------------
# Transcript mirror
# ---------------------------------------------------------------------------

def write_transcript_header(task_id: str, goal: str, source_log: Optional[str]) -> None:
    """Pre-create the per-run transcript mirror with a header.

    `source_log` is the core's live-transcript path; we record it so a
    reader who finds the mirror first can jump to the source. Best-effort.

    The header is rewritten if `source_log` becomes available later (e.g.
    after the core's background dispatch returns the path). Without that
    rewrite the source path would never appear in the file.
    """
    try:
        path = transcript_path(task_id)
        # If the file already has a header AND we don't have a new source
        # to add, skip — a re-write is wasteful and would clobber any
        # lines the user has appended in the meantime.
        if path.exists() and source_log is None:
            return
        header = [
            f"# nightshift run {task_id}",
            "",
            f"- goal: {goal.strip()[:500]}",
            f"- started: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
        ]
        if source_log:
            header.append(f"- source live transcript: {source_log}")
        header.extend([
            "",
            "This file is the per-run mirror of the core's append-only live",
            "transcript (see `source live transcript` above). Lines arrive as",
            "the subagent works — `tail -f` either path and you will see the",
            "same events. Run state lives in `state.json` next to this file.",
            "",
            "---",
            "",
        ])
        # Use a separate rewrite path so the rewrite is atomic w.r.t.
        # any concurrent appends. Append-only filesystems may not
        # support truncate-then-rewrite, but ~/.hermes is regular fs.
        if path.exists() and source_log:
            existing = path.read_text(encoding="utf-8")
            if "- source live transcript:" in existing:
                return  # already annotated
            # Prepend the new source line after the started line, then
            # leave the rest intact.
            lines = existing.splitlines(keepends=True)
            for i, line in enumerate(lines):
                if line.startswith("- started:"):
                    lines.insert(
                        i + 1,
                        f"- source live transcript: {source_log}\n",
                    )
                    break
            path.write_text("".join(lines), encoding="utf-8")
        else:
            path.write_text("\n".join(header), encoding="utf-8")
    except Exception as exc:
        logger.debug("transcript mirror header write failed (%s): %s", task_id, exc)


def append_transcript_line(task_id: str, line: str) -> None:
    """Append one line to the per-run transcript mirror. Best-effort, no raises."""
    try:
        path = transcript_path(task_id)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line if line.endswith("\n") else line + "\n")
    except Exception as exc:
        logger.debug("transcript mirror append failed (%s): %s", task_id, exc)


# ---------------------------------------------------------------------------
# Injection staging
# ---------------------------------------------------------------------------

def write_injection(task_id: str, text: str) -> Optional[str]:
    """Write a user-typed injection to `injections/<ts>.md` and mirror it
    into the transcript. Returns the absolute path of the file written, or
    None on failure.

    Week 2 limitation: Hermes has no public sub-agent conversation hook
    API (only `subagent_start` / `subagent_stop`, which fire at the
    boundary, not mid-run). We therefore stage the text in a directory
    the operator can read, and record a `[INJECT pending]` line in the
    transcript so a watching user sees the request immediately. When the
    core exposes a mid-run hook (tracked in `docs/inject-limitation.md`),
    this function grows the wire-up.
    """
    if not text.strip():
        return None
    ts = time.strftime("%Y%m%d-%H%M%S")
    fname = f"{ts}-{uuid.uuid4().hex[:6]}.md"
    try:
        target = injections_dir(task_id) / fname
        body = (
            f"# nightshift inject @ {time.strftime('%Y-%m-%dT%H:%M:%S')}\n\n"
            f"{text.rstrip()}\n"
        )
        target.write_text(body, encoding="utf-8")
        append_transcript_line(
            task_id,
            f"[{time.strftime('%H:%M:%S')} INJECT pending — see injections/{fname}] {text[:200]}",
        )
        return str(target)
    except Exception as exc:
        logger.debug("nightshift write_injection(%s) failed: %s", task_id, exc)
        return None


# ---------------------------------------------------------------------------
# Live tail follow (best-effort, for the eventual viewer UX)
# ---------------------------------------------------------------------------

def last_transcript_lines(task_id: str, n: int = 40) -> str:
    """Return the last `n` lines of the per-run transcript mirror, or
    a short status string if the run / file is missing."""
    path = transcript_path(task_id)
    if not path.exists():
        st = load_state(task_id)
        if st is None:
            return f"nightshift: no such run {task_id}"
        return f"nightshift: run {task_id} has no transcript yet (status: {st.get('status', '?')})"
    try:
        # Simple bounded read — runs are small enough to slurp.
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        return "".join(lines[-n:])
    except Exception as exc:
        return f"nightshift: failed to read transcript ({exc})"
