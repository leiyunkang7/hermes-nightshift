"""
hermes-nightshift — A Hermes Agent plugin for long-running, interruptible,
transparent autonomous task execution.

Plugin entry point. Hermes calls register(ctx) on plugin load.

Week 2 exposes six commands:

  /nightshift            — dispatch a background delegated task
  /nightshift-status     — list runs, or show one
  /nightshift-tail       — last N lines of a run's transcript
  /nightshift-pause      — request a child to stop
  /nightshift-resume     — clear pause flag (Week 2: marks reschedulable)
  /nightshift-inject     — stage new instructions for a live subagent

Persistence lives in `~/.hermes/nightshift/runs/<task_id>/`; the
human-readable per-run transcript mirror is in `transcript.md` next to
the JSON `state.json` file.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path
from types import ModuleType

logger = logging.getLogger(__name__)

PLUGIN_NAME = "nightshift"
VERSION = "0.2.0"


# ---------------------------------------------------------------------------
# Submodule bootstrap
# ---------------------------------------------------------------------------
# Hermes's plugin loader does `importlib.util.spec_from_file_location`
# on `__init__.py` and `exec_module`s it, but does NOT auto-register
# sibling files into sys.modules. A naive `from nightshift_commands
# import handle_nightshift` therefore resolves to
# `hermes_plugins.nightshift.nightshift_commands` — and that name is
# not in sys.modules, so the import fails with
# `No module named 'nightshift_commands'`.
#
# Workaround: manually pre-load `nightshift_commands.py` and
# `nightshift_state.py` against the same parent package, then the
# `from ... import ...` statements inside them resolve cleanly.
#
# This is the minimum fix consistent with the framework's
# "single-file-or-package" plugin model. We do not patch sys.path.

_PLUGIN_DIR = Path(__file__).resolve().parent
_SUBMODULE_PACKAGE = __name__  # the loaded module's __name__ (e.g. hermes_plugins.nightshift)


def _ensure_submodule(name: str) -> ModuleType:
    """Pre-register `<package>.<name>` against the file
    `<plugin_dir>/<name>.py` so relative imports inside siblings
    succeed. Idempotent — safe to call from `register()` and from
    any other code that wants the submodule's symbols.

    Why we hand-roll this:

    Hermes's plugin loader (see
    ``hermes_cli.plugins.PluginManager._load_directory_module``)
    does ``importlib.util.spec_from_file_location`` on ``__init__.py``
    and ``exec_module``s it. It does **not** auto-register sibling
    files into ``sys.modules``. A naive ``from nightshift_commands
    import ...`` therefore resolves to
    ``hermes_plugins.nightshift.nightshift_commands`` — and that
    name is not in ``sys.modules`` at the time ``__init__.py`` exec
    runs, so the import fails with
    ``No module named 'nightshift_commands'``.

    Even after we manually pre-load the submodule, the spec-loader
    bootstrap still does not resolve ``from <sibling> import ...``
    inside the sibling (Python 3.11 importlib quirk — spec-loaded
    modules do not consult ``sys.modules`` for sibling absolute
    imports). The submodule bodies therefore use
    ``importlib.import_module('hermes_plugins.nightshift.<sibling>')``
    instead of ``from <sibling> import ...``.
    """
    full = f"{_SUBMODULE_PACKAGE}.{name}"
    mod = sys.modules.get(full)
    if mod is not None:
        return mod
    file_path = _PLUGIN_DIR / f"{name}.py"
    if not file_path.exists():
        raise ImportError(f"plugin {PLUGIN_NAME!r} missing submodule file: {file_path}")
    spec = importlib.util.spec_from_file_location(
        full, file_path,
        submodule_search_locations=[str(_PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create module spec for {file_path}")
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = _SUBMODULE_PACKAGE
    mod.__path__ = [str(_PLUGIN_DIR)]  # type: ignore[attr-defined]
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Wire the plugin into Hermes's plugin context.

    Called once on plugin load. Use ctx to:
    - register_command(name, handler, description): add slash commands
    - register_tool(...): expose LLM-callable tools (later weeks)
    - register_hook(event, callback): subscribe to lifecycle events
    """
    # Pre-load the state submodule first (no internal sibling
    # imports), then commands (uses importlib.import_module for its
    # sibling reference). After both are in sys.modules, call into
    # commands.register_commands to wire the six slash commands and
    # install the subagent_stop hook.
    _ensure_submodule("nightshift_state")
    commands_mod = _ensure_submodule("nightshift_commands")

    commands_mod.register_commands(ctx)

    logger.info(
        "plugin %s %s registered (commands: %s)",
        PLUGIN_NAME, VERSION,
        ", ".join([
            "nightshift", "nightshift-status", "nightshift-tail",
            "nightshift-pause", "nightshift-resume", "nightshift-inject",
        ]),
    )
