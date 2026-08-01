"""
hermes-nightshift — A Hermes Agent plugin for long-running, interruptible,
transparent autonomous task execution.

Plugin entry point. Hermes calls register(ctx) on plugin load.

This Week 1 slice exposes ``/nightshift`` only — a synchronous wrapper around
Hermes' built-in ``delegate_task`` that returns the delegated agent's final
summary. Persistence, transcripts, pause/resume/inject, and sub-agent hooks
arrive in later weeks.
"""

PLUGIN_NAME = "nightshift"
VERSION = "0.1.0"


def register(ctx):
    """Wire the plugin into Hermes's plugin context.

    Called once on plugin load. Use ctx to:
    - register_command(name, handler, description): add slash commands
    - register_tool(...): expose LLM-callable tools (later weeks)
    - register_hook(event, callback): subscribe to lifecycle events
    """
    from nightshift_commands import handle_nightshift, handle_nightshift_status

    ctx.register_command(
        name="nightshift",
        description="Delegate one task and return its summary.",
        handler=handle_nightshift,
    )
    ctx.register_command(
        name="nightshift-status",
        description="Show status of running nightshift tasks (Week 2).",
        handler=handle_nightshift_status,
    )
