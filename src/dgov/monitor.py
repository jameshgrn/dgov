"""Lightweight polling daemon for worker state classification and auto-remediation.

Uses local Qwen 4B (localhost:8082) to classify worker outputs and
take automated actions (auto-complete, nudge, idle timeout).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from dgov.backend import get_backend
from dgov.decision import MonitorOutputRequest, ProviderError
from dgov.done import _has_new_commits
from dgov.executor import DagReactor, EscalateResult, RetryResult
from dgov.kernel import DagKernel, WorkerObservation, WorkerPhase
from dgov.monitor_hooks import load_monitor_hooks, match_monitor_hook
from dgov.persistence import (
    STATE_DIR,
    all_panes,
    emit_event,
    get_pane,
    latest_event_id,
    list_active_dag_runs,
    read_events,
    set_pane_metadata,
    take_dispatch_queue,
    update_dag_run,
    wait_for_events,
)
from dgov.status import list_worker_panes, prune_stale_panes, tail_worker_log

if TYPE_CHECKING:
    from dgov.monitor_hooks import MonitorHook

logger = logging.getLogger(__name__)
_MONITOR_WAKE_EVENTS = (
    "dispatch_queued",
    "pane_created",
    "pane_done",
    "pane_failed",
    "pane_timed_out",
    "pane_merged",
    "pane_merge_failed",
    "pane_escalated",
    "pane_superseded",
    "pane_closed",
    "pane_retry_spawned",
    "pane_auto_retried",
    "pane_review_pending",
    "dag_started",
    "dag_resumed",
    "dag_task_dispatched",
    "dag_completed",
    "dag_failed",
    "dag_blocked",
    "merge_completed",
    "monitor_auto_complete",
    "monitor_idle_timeout",
    "monitor_blocked",
    "monitor_auto_merge",
    "monitor_auto_retry",
)

_ACTIVE_ADD_EVENTS = frozenset({"pane_created", "pane_resumed"})
_ACTIVE_REMOVE_EVENTS = frozenset(
    {
        "pane_done",
        "pane_failed",
        "pane_timed_out",
        "pane_merged",
        "pane_merge_failed",
        "pane_escalated",
        "pane_superseded",
        "pane_closed",
    }
)
_MERGE_CLEAR_EVENTS = frozenset(
    {
        "pane_failed",
        "pane_review_pending",
        "pane_timed_out",
        "pane_merged",
        "pane_merge_failed",
        "pane_escalated",
        "pane_superseded",
        "pane_closed",
    }
)
_RETRY_CLEAR_EVENTS = frozenset(
    {
        "pane_done",
        "pane_merged",
        "pane_merge_failed",
        "pane_escalated",
        "pane_superseded",
        "pane_closed",
    }
)


@dataclass
class DagMonitorState:
    run_id: int
    kernel: DagKernel
    reactor: DagReactor


@dataclass
class MonitorLoopState:
    event_cursor: int
    active_slugs: set[str] = field(default_factory=set)
    merge_candidates: set[str] = field(default_factory=set)
    retry_candidates: set[str] = field(default_factory=set)
    active_dags: dict[int, DagMonitorState] = field(default_factory=dict)
    queue_dirty: bool = False


# Deterministic regex patterns for classification
# Order matters: first match wins.
DETERMINISTIC_PATTERNS = {
    "done": [
        r"\b(done|complete[d]?|finish[e]?d?|\bready\b|success|all\.done)",
        r"[\u279c\u2714]\s+[\w\-\.]+\s+git:\([\w\-\.]+\)",  # shell prompt on branch
        r"[\u279c\u2714]\s+[\w\-\.]+\s+%",  # simple zsh prompt
        r"[\u279c\u2714]\s+[\w\-\.]+\s+[\$\#]",  # bash/sh prompt
        r"\(base\)\s+➜",  # conda/zsh combo
        r"\b(finished|exited) with code \d+\b",
    ],
    "waiting_input": [
        r"\b(waiting[ \t]+for\s+(user|input|confirmation|approval|prompt))\b",
        r"\b(paused.*awaiting\b|awaiting.*input\b)",
        r"\bawaiting\s+input\b",
    ],
    "stuck": [
        r"\b(failed|error|exception|traceback|crash|panic|fatal)\b",
    ],
    "committing": [
        r"\b(commit|git\s+add|git\s+commit|pushing|pushed|committed)\b",
    ],
    "idle": [
        r"\b(no[ \t]+work|pause[d]?|idling)\b",
    ],
}


def _classify_deterministic(output: str) -> str | None:
    """Try to classify output using deterministic regex patterns first.

    Returns classification if matched, otherwise None for LLM fallback.
    Called before LLM classification to avoid unnecessary API calls.
    """
    output_lower = output.lower()

    for state, patterns in DETERMINISTIC_PATTERNS.items():
        for pattern in patterns:
            if _regex_match(pattern, output_lower):
                return state

    return None


def _regex_match(pattern: str, text: str) -> bool:
    """Match a regex pattern against text. Input is already lowercased."""

    try:
        return re.search(pattern, text) is not None
    except re.error:
        logger.debug("Invalid regex pattern: %s", pattern)
        return False


def classify_output(
    output: str,
    hooks: list[MonitorHook] | None = None,
    *,
    session_root: str | None = None,
) -> str | tuple[str, MonitorHook]:
    """Classify agent output into working, done, stuck, idle, waiting_input, or committing.

    Uses configurable hooks first, then deterministic regex patterns, and finally
    falls back to LLM for ambiguous cases.
    """
    if not output.strip():
        return "idle"

    # Layer 0: Monitor Hooks (user-configured overrides)
    if hooks:
        matching_hook = match_monitor_hook(output, hooks)
        if matching_hook:
            logger.debug(
                "Monitor hook matched: %s (%s)", matching_hook.kind, matching_hook.pattern
            )
            return ("hook_match", matching_hook)

    # Layer 1: Deterministic classification (fast, no API call)
    deterministic_result = _classify_deterministic(output)
    if deterministic_result is not None:
        logger.debug("Deterministic classification: %s", deterministic_result)
        return deterministic_result

    # Layer 2: LLM classification for ambiguous cases
    try:
        from dgov.decision import DecisionKind
        from dgov.provider_registry import get_provider

        provider = get_provider(DecisionKind.CLASSIFY_OUTPUT, session_root=session_root)
        result = provider.classify_output(MonitorOutputRequest(output=output))
        return result.decision.classification
    except ProviderError as exc:
        logger.debug("Classification failed: %s", exc)
        return "unknown"


def poll_workers(
    project_root: str,
    session_root: str | None = None,
    *,
    panes: list[dict] | None = None,
    hooks: list[MonitorHook] | None = None,
) -> list[dict]:
    """Poll all active worker panes and classify their current state."""
    session_root = session_root or project_root
    workers = (
        panes
        if panes is not None
        else list_worker_panes(
            project_root, session_root, include_freshness=False, include_prompt=False
        )
    )

    active = [w for w in workers if w.get("state") == "active" and not w.get("landing")]
    results = []

    for w in active:
        slug = w["slug"]
        alive = w.get("alive", False)
        output = tail_worker_log(session_root, slug, lines=50)

        if not output:
            classification = "idle"
        else:
            result = classify_output(output, hooks, session_root=session_root)
            classification = result if isinstance(result, str) else result[0]

        # list_worker_panes now includes base_sha; if not, we fallback to get_pane
        base_sha = w.get("base_sha", "")
        if not base_sha:
            raw = get_pane(session_root, slug)
            base_sha = raw.get("base_sha", "") if raw else ""

        branch = w.get("branch") or ""
        has_commits = _has_new_commits(project_root, branch, base_sha)

        # JSON serialize the hook if it matched
        hook_info = None
        if isinstance(classification, tuple):
            _, hook = classification
            hook_info = {
                "pattern": hook.pattern,
                "kind": hook.kind,
                "message": hook.message,
                "keystroke": hook.keystroke,
            }
            classification = "hook_match"
            # Persist to metadata so dgov status can see it (only if changed)
            last_match = w.get("metadata", {}).get("last_hook_match")
            if last_match != hook_info:
                set_pane_metadata(session_root, slug, last_hook_match=hook_info)

        results.append(
            {
                "slug": slug,
                "agent": w.get("agent"),
                "classification": classification,
                "has_commits": has_commits,
                "is_alive": alive,
                "output_preview": output[:100] if output else "",
                "hook_match": hook_info,
            }
        )

    return results


def observe_worker(
    project_root: str,
    session_root: str,
    slug: str,
    *,
    hooks: list[MonitorHook] | None = None,
) -> WorkerObservation:
    """Produce a unified WorkerObservation for a single pane.

    Combines structural signals (done file, exit code, commits, liveness)
    with behavioral classification (output analysis).
    """

    pane = get_pane(session_root, slug)
    if not pane:
        return WorkerObservation(slug=slug, phase=WorkerPhase.UNKNOWN, alive=False)

    pane_state = pane.get("state", "")
    pane_id = pane.get("pane_id", "")
    alive = get_backend().is_alive(pane_id) if pane_id else False
    branch = pane.get("branch_name", "")
    base_sha = pane.get("base_sha", "")
    has_commits = (
        _has_new_commits(project_root, branch, base_sha) if branch and base_sha else False
    )

    done_path = Path(session_root, STATE_DIR, "done", slug)
    exit_path = Path(session_root, STATE_DIR, "done", slug + ".exit")
    has_done = done_path.exists()
    has_exit = exit_path.exists()
    exit_code = None
    if has_exit:
        try:
            exit_code = int(exit_path.read_text().strip())
        except (ValueError, OSError):
            exit_code = -1

    # Structural phase (from signals)
    if pane_state in ("done", "merged"):
        phase = WorkerPhase.DONE
    elif pane_state == "failed":
        phase = WorkerPhase.FAILED
    elif has_done and has_commits:
        phase = WorkerPhase.DONE
    elif has_exit and exit_code == 0 and has_commits:
        phase = WorkerPhase.DONE
    elif has_exit:
        phase = WorkerPhase.FAILED
    elif not alive:
        phase = WorkerPhase.DONE if has_commits else WorkerPhase.FAILED
    else:
        # Alive, no terminal signal — classify output
        output = tail_worker_log(session_root, slug, lines=50)
        if output:
            result = classify_output(output, hooks, session_root=session_root)
            classification = result if isinstance(result, str) else result[0]
        else:
            classification = "idle"
        phase = (
            WorkerPhase(classification)
            if classification in WorkerPhase.__members__.values()
            else WorkerPhase.UNKNOWN
        )

    return WorkerObservation(
        slug=slug,
        phase=phase,
        alive=alive,
        has_commits=has_commits,
        has_done_signal=has_done,
        has_exit_signal=has_exit,
        exit_code=exit_code,
        classification=phase.value,
    )


def _drive_dag(session_root: str, dag_state: DagMonitorState, initial_actions: list) -> None:
    """Recursively process kernel actions and handle immediate event feedback."""
    from dgov.kernel import DagDone, TaskDispatched, TaskRetryStarted

    pending = list(initial_actions)
    while pending:
        action = pending.pop(0)
        if isinstance(action, DagDone):
            # Finalize run in DB
            update_dag_run(session_root, dag_state.run_id, status=action.status)
            emit_event(
                session_root,
                "dag_completed" if action.status == "completed" else "dag_failed",
                f"dag/{dag_state.run_id}",
                dag_run_id=dag_state.run_id,
                status=action.status,
            )
            # Run eval evidence on completion, then always emit evals_verified
            # so --wait only needs to listen for one terminal event type.
            eval_passed = 0
            eval_failed = 0
            eval_total = 0
            if action.status == "completed":
                try:
                    from dgov.plan import verify_eval_evidence

                    results = verify_eval_evidence(
                        session_root,
                        dag_state.run_id,
                        project_root=dag_state.reactor.project_root,
                    )
                    eval_passed = sum(1 for r in results if r["passed"])
                    eval_failed = sum(1 for r in results if not r["passed"])
                    eval_total = len(results)
                except Exception:
                    logger.debug(
                        "eval evidence check failed for dag/%s",
                        dag_state.run_id,
                        exc_info=True,
                    )
            emit_event(
                session_root,
                "evals_verified",
                f"dag/{dag_state.run_id}",
                dag_run_id=dag_state.run_id,
                passed=eval_passed,
                failed=eval_failed,
                total=eval_total,
            )
            # Clean up all panes inline — don't defer to event loop
            try:
                from dgov.persistence import list_dag_tasks

                tasks = list_dag_tasks(session_root, dag_state.run_id)
                for task in tasks:
                    pane_slug = task.get("pane_slug")
                    if pane_slug:
                        try:
                            from dgov.lifecycle import close_worker_pane

                            close_worker_pane(
                                dag_state.reactor.project_root,
                                pane_slug,
                                session_root,
                            )
                        except Exception:
                            logger.debug("pane cleanup failed for %s", pane_slug, exc_info=True)
            except Exception:
                logger.debug("dag cleanup failed for run %s", dag_state.run_id, exc_info=True)
            continue

        event = dag_state.reactor.execute(action)
        if event:
            # Sync kernel state
            if isinstance(event, TaskDispatched):
                dag_state.kernel.pane_slugs[action.task_slug] = event.pane_slug
            elif isinstance(event, TaskRetryStarted):
                dag_state.kernel.pane_slugs[action.task_slug] = event.new_pane_slug

            # Feed event back to kernel
            new_actions = dag_state.kernel.handle(event)
            pending.extend(new_actions)

    # Persist kernel state after pass
    update_dag_run(session_root, dag_state.run_id, state_json=dag_state.kernel.to_dict())


def _load_dag_run(project_root: str, session_root: str, run_dict: dict) -> DagMonitorState | None:
    """Reconstruct a DagMonitorState from a DB record and kickstart if needed."""
    from dgov.dag_parser import DagDefinition, DagFileSpec, DagTaskSpec
    from dgov.kernel import DagState, DagTaskState, TaskWaitDone

    run_id = run_dict["id"]
    try:
        # Reconstruct kernel and reactor
        kernel = DagKernel.from_dict(run_dict["state_json"])

        def_json = run_dict["definition_json"]
        dag_def = DagDefinition(
            name=def_json.get("name", "reconstructed"),
            dag_file=run_dict["dag_file"],
            project_root=project_root,
            session_root=session_root,
            default_max_retries=def_json.get("default_max_retries", 3),
            merge_resolve=def_json.get("merge_resolve", "skip"),
            merge_squash=def_json.get("merge_squash", True),
            max_concurrent=def_json.get("max_concurrent", 0),
            tasks={},
        )
        # Reconstruct tasks
        for t_slug, t_def in def_json.get("tasks", {}).items():
            files = DagFileSpec(
                create=tuple(t_def.get("files", {}).get("create", ())),
                edit=tuple(t_def.get("files", {}).get("edit", ())),
                delete=tuple(t_def.get("files", {}).get("delete", ())),
            )
            dag_def.tasks[t_slug] = DagTaskSpec(
                slug=t_slug,
                summary=t_def.get("summary", ""),
                prompt=t_def.get("prompt", ""),
                commit_message=t_def.get("commit_message", ""),
                agent=t_def.get("agent", ""),
                escalation=tuple(t_def.get("escalation", ())),
                depends_on=tuple(t_def.get("depends_on", ())),
                files=files,
                permission_mode=t_def.get("permission_mode", "bypassPermissions"),
                timeout_s=t_def.get("timeout_s", 600),
                tests_pass=t_def.get("tests_pass", True),
                lint_clean=t_def.get("lint_clean", True),
                post_merge_check=t_def.get("post_merge_check", ""),
                review_agent=t_def.get("review_agent", ""),
                role=t_def.get("role", "worker"),
            )

        reactor = DagReactor(
            project_root=project_root,
            session_root=session_root,
            run_id=run_id,
            dag=dag_def,
        )
        ds = DagMonitorState(run_id, kernel, reactor)

        # Kickstart if new, or reconcile if crashed during wait
        if kernel.state == DagState.IDLE:
            logger.info("Monitor: kickstarting new DAG run %d", run_id)
            _drive_dag(session_root, ds, kernel.start())
        else:
            pending_events = []
            for t_slug, state in kernel.task_states.items():
                if state == DagTaskState.WAITING:
                    p_slug = kernel.pane_slugs.get(t_slug)
                    if p_slug:
                        p_rec = get_pane(session_root, p_slug)
                        if p_rec:
                            p_state = p_rec.get("state")
                            if p_state in ("done", "failed", "timed_out", "merged", "closed"):
                                pending_events.append(TaskWaitDone(t_slug, p_slug, p_state))

            if pending_events:
                logger.info(
                    "Monitor: reconciling %d missed events for DAG %d",
                    len(pending_events),
                    run_id,
                )
                for ev in pending_events:
                    _drive_dag(session_root, ds, kernel.handle(ev))

        return ds
    except Exception:
        logger.warning("Monitor: failed to load DAG run %d", run_id, exc_info=True)
        return None


def _bootstrap_monitor_state(
    project_root: str, session_root: str, *, auto_merge: bool, auto_retry: bool
) -> MonitorLoopState:
    """Seed monitor state from persisted records once at startup."""
    logger.info("Monitor: bootstrapping state from %s", session_root)
    panes = all_panes(session_root)
    logger.info("Monitor: loaded %d panes", len(panes))
    active_dags: dict[int, DagMonitorState] = {}

    runs = list_active_dag_runs(session_root)
    logger.info("Monitor: found %d active DAG runs", len(runs))
    for run in runs:
        ds = _load_dag_run(project_root, session_root, run)
        if ds:
            active_dags[run["id"]] = ds

    state = MonitorLoopState(
        event_cursor=latest_event_id(session_root),
        active_slugs={pane["slug"] for pane in panes if pane.get("state") == "active"},
        merge_candidates={
            pane["slug"] for pane in panes if auto_merge and pane.get("state") == "done"
        },
        retry_candidates={
            pane["slug"]
            for pane in panes
            if auto_retry and pane.get("state") in {"failed", "abandoned"}
        },
        active_dags=active_dags,
        queue_dirty=(Path(session_root) / ".dgov" / "dispatch_queue.jsonl").is_file(),
    )
    logger.info("Monitor: bootstrap complete, cursor at %d", state.event_cursor)
    return state


def _apply_monitor_events(
    project_root: str,
    session_root: str,
    state: MonitorLoopState,
    events: list[dict],
    *,
    auto_merge: bool,
    auto_retry: bool,
) -> None:
    """Update monitor candidate sets from journal events."""
    from dgov.kernel import (
        TaskClosed,
        TaskGovernorResumed,
        TaskMergeDone,
        TaskReviewDone,
        TaskWaitDone,
    )
    from dgov.persistence import get_dag_run

    for event in events:
        state.event_cursor = max(state.event_cursor, int(event.get("id", state.event_cursor)))
        kind = str(event.get("event", ""))
        slug = str(event.get("pane", ""))
        data = json.loads(event.get("data", "{}"))

        if kind == "dispatch_queued":
            state.queue_dirty = True
            continue

        # DAG-level events
        if kind == "dag_started":
            run_id = data.get("dag_run_id")
            if run_id and run_id not in state.active_dags:
                run = get_dag_run(session_root, run_id)
                if run:
                    ds = _load_dag_run(project_root, session_root, run)
                    if ds:
                        state.active_dags[run_id] = ds
            continue

        if kind in ("dag_completed", "dag_failed"):
            run_id = data.get("dag_run_id")
            if run_id in state.active_dags:
                del state.active_dags[run_id]
            # Pane cleanup happens inline in _drive_dag after DagDone.
            continue

        if not slug or slug in {"monitor", "dispatch-queue"}:
            continue

        # Task-level events: find the DAG this pane belongs to
        dag_to_drive = None
        task_slug = None
        for ds in state.active_dags.values():
            for t_slug, p_slug in ds.kernel.pane_slugs.items():
                if p_slug == slug:
                    dag_to_drive = ds
                    task_slug = t_slug
                    break
            if dag_to_drive:
                break

        # Map pane events to DagEvents
        dag_ev = None
        if dag_to_drive and task_slug:
            if kind == "pane_done":
                dag_ev = TaskWaitDone(task_slug, slug, "done")
            elif kind == "pane_failed":
                dag_ev = TaskWaitDone(task_slug, slug, "failed")
            elif kind == "pane_timed_out":
                dag_ev = TaskWaitDone(task_slug, slug, "timed_out")
            elif kind == "review_pass":
                # Deterministic check passed
                dag_ev = TaskReviewDone(task_slug, passed=True, verdict="safe", commit_count=1)
            elif kind == "review_fail":
                # Deterministic check failed
                dag_ev = TaskReviewDone(task_slug, passed=False, verdict="unsafe", commit_count=0)
            elif kind == "merge_completed":
                dag_ev = TaskMergeDone(task_slug)
            elif kind == "pane_closed":
                dag_ev = TaskClosed(task_slug)
            elif kind == "dag_resumed":
                dag_ev = TaskGovernorResumed(task_slug, action=data.get("action", "retry"))

        if dag_to_drive and dag_ev:
            new_actions = dag_to_drive.kernel.handle(dag_ev)
            _drive_dag(session_root, dag_to_drive, new_actions)

        # Legacy monitor remediation logic
        if kind in _ACTIVE_ADD_EVENTS:
            state.active_slugs.add(slug)
        elif kind in _ACTIVE_REMOVE_EVENTS:
            state.active_slugs.discard(slug)

        if kind == "pane_done":
            state.retry_candidates.discard(slug)
            if auto_merge:
                state.merge_candidates.add(slug)
        elif kind in _MERGE_CLEAR_EVENTS:
            state.merge_candidates.discard(slug)

        if kind == "pane_failed":
            state.merge_candidates.discard(slug)
            if auto_retry:
                state.retry_candidates.add(slug)
        elif kind == "monitor_idle_timeout":
            if auto_retry:
                state.retry_candidates.add(slug)
        elif kind in _RETRY_CLEAR_EVENTS:
            state.retry_candidates.discard(slug)


def _tracked_worker_records(
    project_root: str, session_root: str, active_slugs: set[str]
) -> list[dict]:
    """Fetch current pane records for the active slugs the monitor owns."""
    if not active_slugs:
        return []
    workers = list_worker_panes(
        project_root, session_root, include_freshness=False, include_prompt=False
    )
    return [
        worker
        for worker in workers
        if worker.get("slug") in active_slugs and worker.get("state") == "active"
    ]


def _drain_dispatch_queue(project_root: str, session_root: str) -> list[dict]:
    """Dispatch all currently queued prompts once the queue is marked dirty."""
    queued = take_dispatch_queue(session_root)
    actions: list[dict] = []
    for item in queued:
        summary = item.get("summary", "queued task")
        agent = item.get("agent_hint") or "qwen-35b"
        try:
            from dgov.executor import run_dispatch_only

            pane = run_dispatch_only(
                project_root=project_root,
                prompt=summary,
                agent=agent,
                session_root=session_root,
                permission_mode="bypassPermissions",
            )
            logger.info("Monitor: drained queue -> %s (%s)", pane.slug, agent)
            actions.append({"slug": pane.slug, "action": "queue_dispatch"})
        except Exception:
            logger.warning(
                "Monitor: queue dispatch failed for: %s",
                summary,
                exc_info=True,
            )
    return actions


def _process_auto_merge_candidates(
    project_root: str,
    session_root: str,
    state: MonitorLoopState,
    merge_attempted: set[str],
) -> list[dict]:
    """Attempt auto-merge for slugs marked done by the event journal."""
    return _process_candidate_set(
        project_root,
        session_root,
        candidates=state.merge_candidates,
        attempted=merge_attempted,
        valid_states={"done"},
        action_fn=_try_auto_merge,
        on_success=lambda slug: state.active_slugs.discard(slug),
    )


def _resolve_retry_successor_slug(session_root: str, slug: str) -> str | None:
    """Resolve the new pane slug created by retry/escalation side effects."""
    pane_after = get_pane(session_root, slug) or {}
    new_slug = str(pane_after.get("superseded_by", ""))
    if new_slug:
        return new_slug
    for event in reversed(read_events(session_root, slug=slug, limit=5)):
        candidate = str(event.get("new_slug", ""))
        if candidate:
            return candidate
    return None


def _track_retry_successor(state: MonitorLoopState, session_root: str, slug: str) -> None:
    """Track the new active pane created by an auto-retry or escalation."""
    new_slug = _resolve_retry_successor_slug(session_root, slug)
    if new_slug:
        state.active_slugs.add(new_slug)


def _process_auto_retry_candidates(
    project_root: str,
    session_root: str,
    state: MonitorLoopState,
    retry_attempted: set[str],
) -> list[dict]:
    """Attempt auto-retry for failed panes tracked from journal events."""
    return _process_candidate_set(
        project_root,
        session_root,
        candidates=state.retry_candidates,
        attempted=retry_attempted,
        valid_states={"failed", "abandoned"},
        action_fn=_try_auto_retry,
        on_success=lambda slug: _track_retry_successor(state, session_root, slug),
    )


def _process_candidate_set(
    project_root: str,
    session_root: str,
    *,
    candidates: set[str],
    attempted: set[str],
    valid_states: set[str],
    action_fn,
    on_success,
) -> list[dict]:
    """Process a monitor candidate set through a single policy loop."""
    actions: list[dict] = []
    for slug in sorted(candidates):
        pane = get_pane(session_root, slug)
        if not pane or pane.get("state") not in valid_states or slug in attempted:
            candidates.discard(slug)
            continue
        # Skip panes claimed by --land lifecycle
        if pane.get("landing"):
            continue
        try:
            act = action_fn(project_root, session_root, slug)
            if act:
                actions.append({"slug": slug, "action": act})
                print(f"[{time.strftime('%H:%M:%S')}] {act}: {slug}")
                candidates.discard(slug)
                on_success(slug)
            else:
                attempted.add(slug)
                candidates.discard(slug)
        except Exception:
            logger.warning("Auto-action error for %s", slug, exc_info=True)
            attempted.add(slug)
            candidates.discard(slug)
    return actions


def _take_action(project_root: str, session_root: str, worker: dict, history: dict) -> str | None:
    """Evaluate history and take automated action if rules match.

    Only terminal states (done, stuck, idle) trigger remediation.
    Intermediate states (working, waiting_input, committing) are passive,
    but waiting_input can trigger a blocked event.
    """
    slug = worker["slug"]
    classification = worker["classification"]

    # Initialize history entry early so cooldown applies to all actions
    if slug not in history:
        history[slug] = {"classifications": [], "last_action_at": 0.0, "blocked_notified": False}

    hist = history[slug]

    # Handle hook-based actions first
    if classification == "hook_match" and worker.get("hook_match"):
        hook_data = worker["hook_match"]
        kind = hook_data["kind"]
        if kind == "nudge":
            _nudge_stuck(
                project_root,
                session_root,
                slug,
                message=hook_data.get("message"),
                keystroke=hook_data.get("keystroke"),
            )
            hist["last_action_at"] = time.time()
            return "hook_nudge"
        if kind == "fail":
            _mark_idle_failed(project_root, session_root, slug, reason="hook_fail")
            hist["last_action_at"] = time.time()
            return "hook_fail"
        if kind == "auto_complete":
            _auto_complete(project_root, session_root, slug)
            hist["last_action_at"] = time.time()
            return "hook_auto_complete"
        # If it's a state override, treat it as that state for default rules below
        if kind in {"done", "stuck", "idle", "working", "waiting_input", "committing"}:
            classification = kind

    hist["classifications"].append(classification)

    # Keep only last 10 for memory efficiency
    if len(hist["classifications"]) > 10:
        hist["classifications"] = hist["classifications"][-10:]

    # Count consecutive trailing same classifications
    consecutive = 0
    for c in reversed(hist["classifications"]):
        if c == classification:
            consecutive += 1
        else:
            break

    # Re-check state from DB to avoid TOCTOU race
    raw = get_pane(session_root, slug)
    if not raw or raw.get("state") != "active" or raw.get("landing"):
        return None

    # Handle stale workers (active but not alive)
    if not worker.get("is_alive", True):
        if worker.get("has_commits"):
            _auto_complete(project_root, session_root, slug)
            hist["last_action_at"] = time.time()
            return "stale_auto_complete"
        else:
            _mark_idle_failed(project_root, session_root, slug, reason="stale_process")
            hist["last_action_at"] = time.time()
            return "stale_fail"

    # Handle waiting_input (not terminal, but needs notification)
    if classification == "waiting_input":
        if consecutive >= 3 and not hist.get("blocked_notified"):
            emit_event(session_root, "monitor_blocked", slug, reason="waiting_input")
            hist["blocked_notified"] = True
            return "blocked_event"
        return None

    # Reset blocked notification if state changed
    if classification != "waiting_input":
        hist["blocked_notified"] = False

    # Skip remediation for non-terminal states
    if classification in {"working", "committing", "unknown"}:
        return None

    # Cooldown: skip if action was taken recently
    if time.time() - hist["last_action_at"] < 60:
        return None

    # Terminal state rules only
    if classification == "done":
        # If we have commits, one 'done' is enough. If not, wait for 2.
        if worker["has_commits"] or consecutive >= 2:
            _auto_complete(project_root, session_root, slug)
            hist["last_action_at"] = time.time()
            return "auto_complete"

    # Proactive cleanup: if worker has commits but is idling, auto-complete it
    if classification == "idle" and worker["has_commits"]:
        _auto_complete(project_root, session_root, slug)
        hist["last_action_at"] = time.time()
        return "proactive_auto_complete"

    if classification == "stuck" and consecutive >= 3:
        _nudge_stuck(project_root, session_root, slug)
        hist["last_action_at"] = time.time()
        return "nudge"

    if classification == "idle" and consecutive >= 4:
        _mark_idle_failed(project_root, session_root, slug)
        hist["last_action_at"] = time.time()
        return "idle_timeout"

    return None


def _auto_complete(project_root: str, session_root: str, slug: str) -> None:
    """Force a worker to 'done' state via signal file.

    Defense in depth: only complete if there are actual commits beyond base
    when branch/base info is available. If no commits and info is available,
    do nothing (worker may still be working).
    """
    # Get pane record to check branch/base info
    pane = get_pane(session_root, slug)
    if not pane:
        return

    branch_name = pane.get("branch_name", "")
    base_sha = pane.get("base_sha", "")
    project_root_from_pane = pane.get("project_root", "")

    # Require commits when branch/base info is available
    if branch_name and base_sha and project_root_from_pane:
        has_commits = _has_new_commits(project_root_from_pane, branch_name, base_sha)
        if not has_commits:
            # No commits yet — don't auto-complete, worker may still be working
            logger.debug(
                "Monitor: skipping auto-complete for %s — no commits beyond %s",
                slug,
                base_sha[:8],
            )
            return

    done_dir = Path(session_root, STATE_DIR, "done")
    done_dir.mkdir(parents=True, exist_ok=True)
    (done_dir / slug).touch()

    from dgov.executor import run_complete_pane

    result = run_complete_pane(
        project_root, slug, session_root=session_root, reason="auto_complete"
    )
    if not result.changed:
        logger.debug("Monitor: pane %s already in done state", slug)
        return
    logger.info("Monitor: auto-completed %s", slug)


def _nudge_stuck(
    project_root: str,
    session_root: str,
    slug: str,
    message: str | None = None,
    keystroke: str | None = None,
) -> None:
    """Send a nudge message to a stuck worker pane.

    Skips nudging headless workers (interactive agents forced to -p/--prompt
    mode) since they don't read stdin.
    """
    pane = get_pane(session_root, slug)
    if not pane:
        return

    pane_id = pane.get("pane_id")
    if not pane_id:
        return

    # Workers run headless (prompt embedded in launch command) — they don't
    # read stdin, so send-keys nudging is a no-op.  Only LT-GOVs in
    # interactive TUI mode can receive typed input.
    role = pane.get("role", "worker")
    if role == "worker":
        logger.debug("Monitor: skipping nudge for headless worker %s", slug)
        return

    backend = get_backend()
    if keystroke:
        backend.send_input(pane_id, keystroke)
        logger.info("Monitor: nudged worker %s with keystroke", slug)
    else:
        text = (
            message
            or "\n\nYou appear stuck. Commit changes and run: dgov worker complete -m 'summary'\n"
        )
        backend.send_input(pane_id, text)
        logger.info("Monitor: nudged stuck worker %s", slug)

    emit_event(session_root, "monitor_nudge", slug)


def _mark_idle_failed(
    project_root: str, session_root: str, slug: str, reason: str | None = None
) -> None:
    """Mark an idle worker as failed."""
    from dgov.executor import run_fail_pane

    result = run_fail_pane(
        project_root, slug, session_root=session_root, reason=reason or "idle_timeout"
    )
    if not result.changed:
        logger.debug("Monitor: pane %s already in failed state", slug)
        return
    set_pane_metadata(session_root, slug, monitor_reason=reason or "idle_timeout")
    logger.info("Monitor: timed out idle worker %s (reason=%s)", slug, reason)


def _try_auto_merge(project_root: str, session_root: str, slug: str) -> str | None:
    """Attempt to auto-merge a done pane if review verdict is safe."""
    from dgov.executor import run_land_only

    result = run_land_only(project_root, slug, session_root=session_root)
    if result.error:
        log = logger.warning if result.failure_stage == "review_error" else logger.info
        log("Skip auto-merge %s: %s", slug, result.error)
        return None
    if result.merge_result and result.merge_result.get("merged"):
        emit_event(session_root, "monitor_auto_merge", slug)
        logger.info("Monitor: auto-merged %s", slug)
        return "auto_merge"
    logger.warning("Auto-merge failed for %s: %s", slug, result.error)
    return None


def _try_auto_retry(project_root: str, session_root: str, slug: str) -> str | None:
    """Attempt to auto-retry a failed pane using its agent retry policy."""
    from dgov.executor import run_retry_or_escalate

    result = run_retry_or_escalate(project_root, slug, session_root=session_root)
    if not hasattr(result, "new_slug") or not result.new_slug:
        return None
    if isinstance(result, RetryResult):
        emit_event(
            session_root,
            "monitor_auto_retry",
            slug,
            new_slug=result.new_slug,
        )
        logger.info("Monitor: auto-retried %s -> %s", slug, result.new_slug)
        return "auto_retry"
    if isinstance(result, EscalateResult):
        emit_event(
            session_root,
            "monitor_auto_retry",
            slug,
            escalated_to=result.target_agent,
            new_slug=result.new_slug or "",
        )
        logger.info("Monitor: auto-escalated %s -> %s", slug, result.target_agent)
        return "auto_escalate"
    return None


def _wait_for_monitor_wakeup(
    project_root: str, session_root: str, after_id: int, timeout_s: int
) -> list[dict]:
    """Wait for journal activity that should wake the monitor early."""
    if not Path(project_root).is_dir():
        # Fallback to avoid infinite loop if directory gone
        time.sleep(timeout_s)
        return []

    return wait_for_events(
        session_root,
        after_id=after_id,
        event_types=_MONITOR_WAKE_EVENTS,
        timeout_s=float(timeout_s),
    )


def ensure_monitor_running(project_root: str, session_root: str | None = None) -> None:
    """Ensure the headless monitor daemon is running in the background.

    Uses flock to probe whether a monitor already holds the lock.
    If the lock is held, another monitor is alive — do nothing.
    If the lock is free, spawn a new monitor (which will acquire it on startup).
    """
    import fcntl
    import os
    import shutil
    import subprocess
    import sys

    session_root = os.path.abspath(session_root or project_root)
    project_root = os.path.abspath(project_root)
    lock_path = Path(session_root) / ".dgov" / "monitor.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Probe: try to acquire the lock non-blocking
    try:
        probe_fd = open(lock_path, "w")  # noqa: SIM115
        fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # We got the lock — no monitor running. Release so the spawned one can acquire.
        fcntl.flock(probe_fd, fcntl.LOCK_UN)
        probe_fd.close()
    except OSError:
        # Lock held — a monitor is already running
        return

    # No monitor running — spawn one
    if True:
        if getattr(sys, "frozen", False):
            cmd = [
                sys.executable,
                "monitor",
                "-r",
                project_root,
                "--session-root",
                session_root,
            ]
        elif shutil.which("uv"):
            cmd = [
                "uv",
                "run",
                "dgov",
                "monitor",
                "-r",
                project_root,
                "--session-root",
                session_root,
            ]
        elif shutil.which("dgov"):
            cmd = [
                "dgov",
                "monitor",
                "-r",
                project_root,
                "--session-root",
                session_root,
            ]
        else:
            raise RuntimeError(
                "Cannot launch headless monitor: neither `uv` nor `dgov` is available on PATH"
            )

        log_dir = Path(session_root) / ".dgov" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "monitor.log"

        with open(log_file, "a") as f:
            proc = subprocess.Popen(
                cmd,
                stdout=f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                cwd=project_root,
            )
        logger.info(
            "Monitor: kickstarted headless daemon for %s (pid=%d, logging to %s)",
            project_root,
            proc.pid,
            log_file,
        )


def run_monitor(
    project_root: str,
    session_root: str | None = None,
    *,
    poll_interval: int = 5,
    dry_run: bool = False,
    auto_merge: bool = False,
    auto_retry: bool = True,
) -> None:
    """Run the monitor loop."""
    import fcntl

    session_root = session_root or project_root

    # flock singleton — only one monitor per session_root
    lock_path = Path(session_root, STATE_DIR, "monitor.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(lock_path, "w")  # noqa: SIM115
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.info("Another monitor is already running, exiting")
        lock_fd.close()
        return
    # Write PID for diagnostics only (flock is the real liveness check)
    import os as _os

    lock_fd.write(str(_os.getpid()))
    lock_fd.flush()

    # Ensure logging is configured for console output
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    state = _bootstrap_monitor_state(
        project_root, session_root, auto_merge=auto_merge, auto_retry=auto_retry
    )
    history: dict[str, dict] = {}
    merge_attempted: set[str] = set()
    retry_attempted: set[str] = set()
    pending_events: list[dict] = []

    monitor_dir = Path(session_root, STATE_DIR, "monitor")
    monitor_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Starting monitor on %s", project_root)
    print("Monitor active: event-driven (per-process notify pipes)")

    last_prune = 0.0
    try:
        while True:
            # Safety: ensure project_root still exists (daemon might have started in a deleted dir)
            if not Path(project_root).is_dir():
                logger.error("Monitor: project_root %s is gone! Stopping.", project_root)
                break

            try:
                _apply_monitor_events(
                    project_root,
                    session_root,
                    state,
                    pending_events,
                    auto_merge=auto_merge,
                    auto_retry=auto_retry,
                )

                # Reload hooks each tick for live updates
                hooks = load_monitor_hooks(session_root)

                # Prune stale panes periodically
                now = time.time()
                if now - last_prune > 120:  # Every 2 minutes
                    pruned = prune_stale_panes(project_root, session_root)
                    if pruned:
                        logger.info("Monitor: pruned stale panes: %s", ", ".join(pruned))
                    last_prune = now

                actions = []

                if state.queue_dirty:
                    queue_actions = _drain_dispatch_queue(project_root, session_root)
                    actions.extend(queue_actions)
                    for queue_action in queue_actions:
                        state.active_slugs.add(queue_action["slug"])
                    state.queue_dirty = False

                tracked_workers = _tracked_worker_records(
                    project_root, session_root, state.active_slugs
                )
                workers = poll_workers(
                    project_root,
                    session_root,
                    panes=tracked_workers,
                    hooks=hooks,
                )

                for w in workers:
                    action = _take_action(project_root, session_root, w, history)
                    if action:
                        actions.append({"slug": w["slug"], "action": action})
                        print(f"[{time.strftime('%H:%M:%S')}] Action: {action} -> {w['slug']}")
                        if action in {
                            "auto_complete",
                            "stale_auto_complete",
                            "proactive_auto_complete",
                            "hook_auto_complete",
                        }:
                            state.active_slugs.discard(w["slug"])
                            if auto_merge:
                                state.merge_candidates.add(w["slug"])
                        elif action in {"stale_fail", "idle_timeout", "hook_fail"}:
                            state.active_slugs.discard(w["slug"])
                            if auto_retry:
                                state.retry_candidates.add(w["slug"])

                if auto_merge:
                    actions.extend(
                        _process_auto_merge_candidates(
                            project_root,
                            session_root,
                            state,
                            merge_attempted,
                        )
                    )
                if auto_retry:
                    actions.extend(
                        _process_auto_retry_candidates(
                            project_root,
                            session_root,
                            state,
                            retry_attempted,
                        )
                    )

                status = {
                    "timestamp": time.time(),
                    "workers": workers,
                    "actions": actions,
                }

                with open(monitor_dir / "status.json", "w") as f:
                    json.dump(status, f, indent=2)

                if workers:
                    worker_states = ", ".join(
                        f"{w['slug']}={w['classification']}" for w in workers
                    )
                    emit_event(session_root, "monitor_tick", "monitor", states=worker_states)

            except Exception:
                logger.warning("Monitor tick failed", exc_info=True)

            if dry_run:
                return

            pending_events = _wait_for_monitor_wakeup(
                project_root,
                session_root,
                state.event_cursor,
                poll_interval,
            )
    except KeyboardInterrupt:
        logger.info("Monitor stopped by user")
        print("\nMonitor stopped.")
