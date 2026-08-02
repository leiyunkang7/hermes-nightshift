# Hermes Nightshift

> A Hermes Agent plugin that runs long-lived delegated tasks with persistence,
> visible transcripts, and pause/resume/inject controls on top of vanilla
> `delegate_task`.

## What is this?

`hermes-nightshift` is a plugin for [Hermes Agent](https://hermes-agent.nousresearch.com)
that adds a `/nightshift` family of slash commands on top of Hermes' built-in
`delegate_task` tool. Week 1 delivered the synchronous `/nightshift` slice.
Week 2 turns that into an asynchronous, persistent, observable workflow:

| Command | Purpose |
|---|---|
| `/nightshift "<goal>"` | Dispatch a delegated task **in the background**, return its `task_id` immediately |
| `/nightshift-status [id]` | List all runs, or show one |
| `/nightshift-tail <id> [n]` | Last `n` lines of a run's transcript mirror (default 40) |
| `/nightshift-pause <id>` | Request a single subagent to stop at its next iteration boundary |
| `/nightshift-resume <id>` | Mark a paused run reschedulable (Week 3 will re-dispatch automatically) |
| `/nightshift-inject <id> "text"` | Stage instructions for a live subagent (see `docs/inject-limitation.md`) |

The Week 1 synchronous `/nightshift "do thing"; get summary` flow is replaced
by: `/nightshift "do thing"` returns the `task_id`; you watch progress with
`/nightshift-tail` and stop with `/nightshift-pause`. The full transcript
streams both to the core's cache (`cache/delegation/live/<id>/task-0.log`)
and to a per-run mirror under `~/.hermes/nightshift/runs/<task_id>/transcript.md`
— see [Watching a run in real time](#watching-a-run-in-real-time) below.

## Watching a run in real time

The per-run transcript mirror at
`~/.hermes/nightshift/runs/<task_id>/transcript.md` is plain Markdown that
grows as the subagent works. To watch it live:

    tail -F ~/.hermes/nightshift/runs/<task_id>/transcript.md

For two concurrent runs, list both files:

    tail -qF ~/.hermes/nightshift/runs/ns_a1b2c3d4/transcript.md \
              ~/.hermes/nightshift/runs/ns_e5f6g7h8/transcript.md

If you run Hermes in tmux, this is most natural as a split pane:

    tmux split-window -h 'tail -F ~/.hermes/nightshift/runs/<task_id>/transcript.md'

`tail` may print `tail: ...: file truncated` once at run start; this is
benign — `write_transcript_header` rewrites the file once to splice in
the source-log annotation, and `tail -F` recovers.

To pause or inject from the viewer, switch focus back to the chat pane
and run `/nightshift-pause <task_id>` or
`/nightshift-inject <task_id> "new instructions"`.

## Status

**Week 2 of an active development project.**

- Goal: `/nightshift` runs a delegated task end-to-end, persists across the
  CLI turn, and stays inspectable / interruptible while it runs
- See the [wayfinder map (issue #1)](https://github.com/leiyunkang7/hermes-nightshift/issues/1)
  for destination, decisions, and fog-of-war
- See the [project board](https://github.com/users/leiyunkang7/projects/3) for live ticket status

## How it works

```
/nightshift "research 5 agent business models"
  ↓
Plugin: register_commands binds ctx, registers slash commands
Plugin: handle_nightshift writes state.json + transcript.md
Plugin: ctx.dispatch_tool("delegate_task", background=True, ...)
  ↓
Core: child agent spawned, runs on background executor
Core: LiveTranscriptWriter streams events to cache/delegation/live/<id>/task-0.log
Core: completion re-enters the parent session as a single message
Plugin: subagent_stop hook fires → updates state.json with final status
  ↓
User (any time): /nightshift-tail <id>
User (any time): /nightshift-pause  <id>   →  interrupt_subagent(deleg_id)
User (any time): /nightshift-inject <id> "text"  →  staged in injections/<ts>.md
```

The plugin does NOT modify `delegate_task` itself; it rides on top of the
core's existing `background=True` path, the `LiveTranscriptWriter` side
channel, and the `subagent_start` / `subagent_stop` lifecycle hooks.

## Plugin layout

```
~/.hermes/plugins/nightshift/
├── plugin.yaml            # manifest
├── __init__.py            # register() — pre-loads siblings, wires commands
├── nightshift_commands.py # command handlers + ctx binding
├── nightshift_state.py    # run state store + transcript mirror + inject staging
├── docs/
│   └── inject-limitation.md  # Week 2 inject scope (and Week 3 plan)
└── tests/
    └── test_week2.py      # 21 behavior tests
```

## State on disk

Per run: `~/.hermes/nightshift/runs/<task_id>/`

- `state.json` — `{status, goal, started_at, delegation_id, ...}`
- `transcript.md` — append-only mirror of the core's live transcript
- `injections/<ts>-<rand>.md` — staged user instructions

Old runs are pruned opportunistically (50 most recent kept).

## Install for local testing

```bash
git clone https://github.com/leiyunkang7/hermes-nightshift
cd hermes-nightshift

# Symlink into the user plugin directory
mkdir -p ~/.hermes/plugins
ln -sfn "$(pwd)" ~/.hermes/plugins/nightshift

# Enable the plugin
hermes plugins enable nightshift
hermes plugins enable nightshift --allow-tool-override
```

The `--allow-tool-override` flag is only required if the plugin ever overrides
a built-in tool; Week 2 does not, so it can be skipped.

## Use

Start (or restart) Hermes, then:

```
/nightshift "research 5 AI agent business models worth pursuing in 2026"
```

The command returns immediately with a `task_id`. To follow progress:

```
/nightshift-tail <task_id>
/nightshift-status <task_id>
/nightshift-pause <task_id>
/nightshift-inject <task_id> "switch to focus on solo-developer models"
```

The same transcript is also available at
`cache/delegation/live/<delegation_id>/task-0.log` (the core's source) and
at `~/.hermes/nightshift/runs/<task_id>/transcript.md` (the per-run mirror).

## Known limitations

- **`/nightshift-inject`** stages the text to disk but does not push it into
  the active child's next turn. See [`docs/inject-limitation.md`](docs/inject-limitation.md)
  for the Week 3 plan.
- **`/nightshift-resume`** marks a paused run as reschedulable; it does not
  auto-rerun. Re-dispatch with `/nightshift` for now.
- **One process only.** Run state lives on disk so you can introspect past
  runs, but active subagent control (`/nightshift-pause`) only works when
  the subagent is registered with the same hermes process that issued
  `/nightshift`. Process restart loses the in-memory active-subagent
  registry; runs marked `pause_requested` are recorded but the core
  has no record to interrupt.

## Development

```bash
# Run the Week 2 command behavior tests
python3 -c "
import importlib.util, pathlib
spec = importlib.util.spec_from_file_location('t', pathlib.Path('tests/test_week2.py'))
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
mod._run_all_tests()
"
```

## License

MIT
