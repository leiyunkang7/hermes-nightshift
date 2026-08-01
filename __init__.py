"""
hermes-nightshift — A Hermes Agent plugin for long-running, interruptible,
transparent autonomous task execution.

Plugin entry point. Hermes calls register(ctx) on plugin load.

Phase 0 (week 1-2): minimal viable slash command
Phase 1 (week 2-3): transcript writer + pause/resume/inject
Phase 2 (week 3-4): subagent hooks for cluster awareness
"""

PLUGIN_NAME = "nightshift"
VERSION = "0.1.0"


def register(ctx):
    """Wire the plugin into Hermes's plugin context.

    Called once on plugin load. Use ctx to:
    - register_tool(...): expose LLM-callable tools
    - register_hook(event, callback): subscribe to lifecycle events
    - register_command(name, handler, description): add slash commands
    - inject_message(content, role): push messages into the active session
    - llm.complete(...): run a host-owned LLM call
    """
    # Phase 0: register the slash command(s). Tool schemas and handlers
    # live in tools.py / schemas.py — added once we have working code.
    from tools import handle_nightshift, handle_nightshift_status
    from intervention import handle_pause, handle_resume, handle_inject

    ctx.register_command(
        name="nightshift",
        description="Run a long-running task with visibility and intervention hooks.",
        handler=handle_nightshift,
    )
    ctx.register_command(
        name="nightshift-status",
        description="Show status of running nightshift tasks.",
        handler=handle_nightshift_status,
    )
    ctx.register_command(
        name="nightshift-pause",
        description="Pause a running nightshift task.",
        handler=handle_pause,
    )
    ctx.register_command(
        name="nightshift-resume",
        description="Resume a paused nightshift task.",
        handler=handle_resume,
    )
    ctx.register_command(
        name="nightshift-inject",
        description="Inject new instructions into a running task.",
        handler=handle_inject,
    )

    # Phase 2 hooks — registered now so they fire when subagents spawn,
    # even before we build the transcript viewer.
    from hooks import on_subagent_start, on_subagent_stop

    ctx.register_hook("subagent_start", on_subagent_start)
    ctx.register_hook("subagent_stop", on_subagent_stop)
