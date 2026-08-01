# Hermes Nightshift

> A Hermes Agent plugin that exposes `/nightshift` for one-shot delegated tasks.

## What is this?

`hermes-nightshift` is a plugin for [Hermes Agent](https://hermes-agent.nousresearch.com) that adds a `/nightshift` slash command on top of Hermes' built-in `delegate_task` tool. Week 1 delivers one capability only: type `/nightshift "<goal>"` and the command delegates that goal to a single Hermes agent, blocks until it finishes, and returns the agent's final summary.

Later slices will add persistence, real-time transcripts, and pause/resume/inject. They are intentionally **not** part of the current implementation.

## Status

**Week 1 of an active development project.**

- Goal: first make `/nightshift "<goal>"` run one delegated task end-to-end
- See the [wayfinder map (issue #1)](https://github.com/leiyunkang7/hermes-nightshift/issues/1) for destination, decisions, and fog-of-war
- See the [project board](https://github.com/users/leiyunkang7/projects/3) for live ticket status

## Plugin layout

```
~/.hermes/plugins/nightshift/
├── plugin.yaml            # manifest
├── __init__.py            # register() — wires slash commands
├── nightshift_commands.py # Week 1 command handler
└── tests/                 # command behavior tests
```

## Install for local testing

```bash
# Clone the repo
git clone https://github.com/leiyunkang7/hermes-nightshift
cd hermes-nightshift

# Symlink into the user plugin directory
mkdir -p ~/.hermes/plugins
ln -sfn "$(pwd)" ~/.hermes/plugins/nightshift

# Enable the plugin and allow tool-override
hermes plugins enable nightshift
hermes plugins enable nightshift --allow-tool-override
```

The `--allow-tool-override` flag is only required if the plugin ever overrides a built-in tool; Week 1 does not, so it can be skipped until the plugin grows new tools.

## Use

Start (or restart) Hermes, then:

```
/nightshift "research 5 AI agent business models worth pursuing in 2026"
```

The command blocks until the delegated task finishes. `/nightshift-status` is a Week 1 placeholder that returns a "not implemented yet" message; it will be wired up to the live task store in Week 2.

## Development

```bash
# Run the command behavior tests
python3 -c "
import importlib.util, pathlib
spec = importlib.util.spec_from_file_location('t', pathlib.Path('tests/test_week1_command.py'))
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
for n in sorted(x for x in dir(mod) if x.startswith('test_')):
    getattr(mod, n)()
    print('PASS', n)
"
```

## License

MIT
