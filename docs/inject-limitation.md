# `/nightshift-inject` — Week 2 limitation

The Week 2 acceptance criterion for the inject command was:

> `/nightshift-inject <task_id> "new instructions"` injects text into the
> active sub-agent's next turn

We **partially** meet that bar. What Week 2 does:

1. Writes the text to `~/.hermes/nightshift/runs/<task_id>/injections/<ts>.md`
   (one file per call; files are append-only and never deleted during
   the run's lifetime).
2. Appends a `[HH:MM:SS INJECT pending — see injections/<file>]` line to
   the per-run transcript mirror so a watching user sees the request
   immediately.
3. Rejects the call when the run is not live (status must be one of
   `running`, `dispatching`, `pausing`, `pause_requested`).

What Week 2 does **not** do:

* Push the text into the active child's *next* model turn. The text
  sits on disk; an operator can read it, copy it, and feed it manually
  via the TUI or paste it into the child's next dispatch if the child
  is re-runnable.

## Why

Hermes exposes only two sub-agent lifecycle hooks:

* `subagent_start` — fires when a child agent starts
* `subagent_stop`  — fires when a child agent stops

Neither gives the plugin access to a child's in-flight conversation
turns. There is no public `pre_subagent_iteration`,
`on_subagent_message`, or `subagent_text_stream` hook. Searched the
core (PR grep + `agent/`, `tools/`, `gateway/`):

* `agent/subagent_lifecycle.py` is the lifecycle module — it manages
  parent/child binding and provides `bind_subagent_parent()` /
  `get_active_subagent_parent()` for the framework's own use, but
  exposes no plugin-registered callback surface mid-run.
* `AIAgent._active_children` is a list of running child agents the
  parent owns, but it lives on the agent instance and has no plugin
  accessor.
* The only "mid-run" extension point is `child.tool_progress_callback`,
  which the core uses for `LiveTranscriptWriter` (a one-way tap, not
  a writable interface). Reusing it for injection would silently
  confuse the live-transcript system.
* `interrupt_subagent()` exists, but it is a stop signal, not a
  message injector.

## What this means for users in Week 2

When a subagent is in the middle of a long task and the user wants to
redirect it:

1. `/nightshift-pause <task_id>` — request the child to stop at its
   next iteration boundary.
2. The child finishes its current turn, then stops.
3. `/nightshift-inject <task_id> "new instructions"` — stage the new
   text.
4. `/nightshift-resume <task_id>` — mark the run reschedulable.
5. Re-dispatch with `/nightshift "<new goal>"`.

That loop covers the "user wants to redirect" case at the cost of one
extra re-dispatch. It is not as smooth as Week 3's planned mid-run
inject, but it does not lose context — the transcript mirror, the
state file, and the staged injection file all stay on disk and are
visible to a human operator throughout.

## What Week 3 did

Three options were ranked in this document; route (3) was selected in
[issue #7](https://github.com/leiyunkang7/hermes-nightshift/issues/7) and
shipped in [issue #8](https://github.com/leiyunkang7/hermes-nightshift/issues/8):

> **Ship a "rerun with prior transcript as context" mode that
> re-dispatches with the old run's transcript attached as `context=` —
> semantically weaker but mechanically straightforward.**

`/nightshift-resume <task_id>` now mints a new task_id under the
generation-counter rule, copies the parent's `transcript.md` into the
new run dir as `prior_transcript.md`, re-dispatches via `delegate_task`
with `context=prior_transcript_body`, and links the new run back to
the parent via `resumed_from`. See the README §Known limitations for
the user-facing description.

Routes (1) and (2) remain future uplifts: if Hermes exposes a
`pre_subagent_iteration` hook (route 1) or a writable post-processor on
`_active_children` (route 2), the staging layer in
`nightshift_state.write_injection` grows a new branch and the redirect
becomes true mid-run inject rather than pause-and-rerun.
