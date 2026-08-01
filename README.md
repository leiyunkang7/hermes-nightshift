# Hermes Nightshift

> **A Hermes Agent plugin for long-running, interruptible, transparent autonomous task execution.**

## What is this?

`hermes-nightshift` is a plugin for [Hermes Agent](https://hermes-agent.nousresearch.com) that lets you delegate long-running tasks to an agent cluster — with three properties that vanilla `delegate_task` doesn't give you:

1. **Persistence** — tasks run for hours, not minutes. State is durable across crashes.
2. **Visibility** — see exactly what every sub-agent is doing, in real time, in a human-readable transcript.
3. **Intervention** — pause, resume, inject new direction, or kill a branch mid-run without losing context.

## Why does this matter?

Most agent runtimes (including vanilla Hermes) treat each turn as a discrete interaction: ask → answer. `hermes-nightshift` treats a delegated task as a **long-lived session** with the user as the supervisor. The agent does the work; you stay in control.

## Status

**Active development.** 30-day MVP window started 2026-08-01.

- Goal: ship a usable `nightshift` slash command + transcript viewer + intervention panel
- See the [wayfinder map](https://github.com/leiyunkang7/hermes-nightshift/issues/1) for weekly milestones
- See the [project board](https://github.com/leiyunkang7/hermes-nightshift/projects/1) for live progress

## Plugin layout

```
~/.hermes/plugins/nightshift/
├── plugin.yaml          # manifest
├── __init__.py          # register() — wires schemas to handlers
├── schemas.py           # tool schemas (what the LLM sees)
├── tools.py             # tool handlers (what runs when called)
└── nightshift/          # runtime
    ├── runner.py        # background task loop
    ├── transcript.py    # append-only transcript writer
    └── intervention.py  # pause/resume/inject protocol
```

## Quick start

```bash
# Install the plugin (once it's published)
mkdir -p ~/.hermes/plugins
cp -r nightshift ~/.hermes/plugins/

# Enable it
hermes plugins enable nightshift

# Use it
/nightshift "research 5 AI agent business models worth pursuing in 2026"
```

## Development

```bash
git clone https://github.com/leiyunkang7/hermes-nightshift
cd hermes-nightshift
# Install locally for testing
mkdir -p ~/.hermes/plugins
ln -s "$(pwd)" ~/.hermes/plugins/nightshift
```

## License

MIT
