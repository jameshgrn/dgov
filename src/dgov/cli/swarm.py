"""Swarm coordination commands.

Phase 1 skeleton for think/convo/watch subcommands.
"""

import click

from dgov.backend import get_backend


@click.group()
def swarm():
    """Orchestrate multi-agent swarms."""
    pass


@swarm.command("think")
@click.option("--session", "-s", required=True, help="Session root directory")
@click.option("--agent", "-a", default="qwen-35b", help="Agent to orchestrate")
@click.option("--prompt", "-p", required=True, help="Think prompt")
@click.option("--max-steps", type=int, default=10, help="Max thinking steps")
def think(session: str, agent: str, prompt: str, max_steps: int) -> None:
    """Start a think session for structured reasoning."""
    from datetime import datetime, timezone

    backend = get_backend()
    pane_id = backend.create_pane(
        f"[think] {agent}",
        f"Think about: {prompt[:50]}...",
        cwd=session,
    )
    ts = datetime.now(timezone.utc).isoformat()
    backend.send_to_pane(pane_id, f"THINK_START\n\n{prompt}\n\nMax steps: {max_steps}")
    click.echo(f'{{"pane_id": "{pane_id}", "status": "started", "ts": "{ts}"}}')


@swarm.command("convo")
@click.option("--session", "-s", required=True, help="Session root directory")
@click.option("--agents", "-a", multiple=True, required=True, help="Agent slugs (can repeat)")
@click.option("--prompt", "-p", help="Initial message to send")
def convo(session: str, agents: tuple[str, ...], prompt: str | None) -> None:
    """Start a conversation between agents."""
    from datetime import datetime, timezone

    if not agents:
        click.echo("Error: at least one agent required", err=True)
        return

    backend = get_backend()
    pane_id = backend.create_pane(
        f"[convo] {' + '.join(agents)}",
        f"Conversation with: {' + '.join(agents)}",
        cwd=session,
    )
    ts = datetime.now(timezone.utc).isoformat()
    if prompt:
        backend.send_to_pane(pane_id, f"CONVO_START\n\nAgents: {', '.join(agents)}\n\n{prompt}")
    click.echo(f'{{"pane_id": "{pane_id}", "status": "started", "ts": "{ts}"}}')


@swarm.command("watch")
@click.option("--session", "-s", required=True, help="Session root directory")
@click.option("--pattern", "-p", required=True, help="Pattern to watch")
@click.option("--threshold", type=float, default=0.8, help="Alert threshold")
def watch(session: str, pattern: str, threshold: float) -> None:
    """Start watching for events matching a pattern."""
    from datetime import datetime, timezone

    backend = get_backend()
    pane_id = backend.create_pane(
        f"[watch] {pattern}",
        f"Watching for: {pattern} (threshold: {threshold})",
        cwd=session,
    )
    ts = datetime.now(timezone.utc).isoformat()
    backend.send_to_pane(pane_id, f"WATCH_START\n\nPattern: {pattern}\nThreshold: {threshold}")
    click.echo(f'{{"pane_id": "{pane_id}", "status": "started", "ts": "{ts}"}}')
