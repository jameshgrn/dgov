"""Swarm coordination commands.

Phase 1 skeleton for think/convo/watch subcommands.
"""

import json
import time

import click


@click.group()
def swarm():
    """Orchestrate multi-agent swarms."""
    pass


@swarm.command("think")
@click.option("--session", "-s", required=True, help="Session root directory")
@click.option("--agent", "-a", default="qwen-35b", help="Agent to orchestrate")
@click.option("--prompt", "-p", required=True, help="Think prompt")
@click.option("--max-steps", type=int, default=10, help="Max thinking steps")
@click.option("--parent", "-P", default="", help="Parent pane slug (for chaining)")
def think(session: str, agent: str, prompt: str, max_steps: int, parent: str) -> None:
    """Start a think session for structured reasoning."""
    from datetime import datetime, timezone

    from dgov.lifecycle import create_worker_pane
    from dgov.persistence import emit_event

    pane = create_worker_pane(
        project_root=session,
        prompt=f"{prompt}\n\nMax steps: {max_steps}",
        agent=agent,
        role="reasoner",
        parent_slug=parent,
        session_root=session,
    )
    ts = datetime.now(timezone.utc).isoformat()
    emit_event(session, "think_started", pane.slug, agent=agent, prompt=prompt[:200])
    click.echo(
        f'{{"slug": "{pane.slug}", "pane_id": "{pane.pane_id}", '
        f'"status": "started", "ts": "{ts}"}}'
    )


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

    from dgov.lifecycle import create_worker_pane
    from dgov.persistence import emit_event

    # Create conversation host pane
    host_prompt = "\n".join(
        [
            "You are a conversation host orchestrating dialogue between agents.",
            f"Participants: {' + '.join(agents)}",
            "",
            "Your role: route messages between participants, maintain context, and summarize when needed.",
        ]
    )
    if prompt:
        host_prompt += f"\n\nInitial message:\n{prompt}"

    host_pane = create_worker_pane(
        project_root=session,
        prompt=host_prompt,
        agent="qwen-35b",
        role="conversation_host",
        session_root=session,
    )

    # Launch participant panes linked to the host
    participant_slugs = []
    for agent in agents:
        part_prompt = (
            f"You are participating in a conversation hosted by {host_pane.slug}.\n"
            f"Role: {agent}\n\n"
            "Follow the host's routing instructions and respond to your turn."
        )
        part_pane = create_worker_pane(
            project_root=session,
            prompt=part_prompt,
            agent=agent,
            role="participant",
            parent_slug=host_pane.slug,
            session_root=session,
        )
        participant_slugs.append(part_pane.slug)

    ts = datetime.now(timezone.utc).isoformat()
    emit_event(
        session,
        "convo_started",
        host_pane.slug,
        agents=list(agents),
        host_slug=host_pane.slug,
        participant_slugs=participant_slugs,
    )
    click.echo(
        json.dumps(
            {
                "host_slug": host_pane.slug,
                "pane_id": host_pane.pane_id,
                "participants": participant_slugs,
                "status": "started",
                "ts": ts,
            }
        )
    )


@swarm.command("watch")
@click.option("--session", "-s", required=True, help="Session root directory")
@click.option("--slug", "-t", required=True, help="Target slug to watch")
@click.option("--pattern", "-p", required=True, help="Event pattern to match")
@click.option("--threshold", type=float, default=0.8, help="Alert threshold")
@click.option("--interval", type=float, default=1.0, help="Polling interval in seconds")
def watch(session: str, slug: str, pattern: str, threshold: float, interval: float) -> None:
    """Start watching for events matching a pattern."""
    from datetime import datetime, timezone

    from dgov.lifecycle import create_worker_pane
    from dgov.persistence import emit_event, read_events

    # Create watcher pane
    watcher_prompt = (
        f"You are monitoring events for the target slug '{slug}'.\n"
        f"Watch for events matching pattern: {pattern}\n"
        f"Alert threshold: {threshold}\n\n"
        "Poll periodically and report any matching events."
    )
    watcher_pane = create_worker_pane(
        project_root=session,
        prompt=watcher_prompt,
        agent="qwen-4b",
        role="watcher",
        session_root=session,
    )

    ts = datetime.now(timezone.utc).isoformat()
    emit_event(
        session,
        "watch_started",
        watcher_pane.slug,
        target_slug=slug,
        pattern=pattern,
        threshold=threshold,
        interval=interval,
    )
    click.echo(
        json.dumps(
            {
                "slug": watcher_pane.slug,
                "pane_id": watcher_pane.pane_id,
                "target": slug,
                "pattern": pattern,
                "status": "started",
                "ts": ts,
            }
        )
    )

    # Basic polling loop
    try:
        while True:
            events = read_events(session, slug=slug)
            for ev in events:
                if pattern in ev.get("event", ""):
                    click.echo(f"[WATCH] {ev['ts']} - {ev['event']}: {ev.get('data', '')}")
            time.sleep(interval)
    except KeyboardInterrupt:
        emit_event(session, "watch_stopped", watcher_pane.slug, target_slug=slug)
        click.echo("\n[WATCH] Stopped.")
