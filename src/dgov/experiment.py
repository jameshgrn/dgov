"""Autoresearch-style experiment loops.

Each experiment dispatches a worker pane, waits for results, evaluates the
metric, and decides to merge (accept) or discard (reject).  The loop runs
sequentially so the baseline is always clean.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from dgov.persistence import _STATE_DIR, _emit_event

# ---------------------------------------------------------------------------
# Experiment log
# ---------------------------------------------------------------------------

_EXPERIMENTS_DIR = "experiments"
_RESULTS_DIR = "results"


class ExperimentLog:
    """Reads/writes a JSONL experiment log at .dgov/experiments/<program>.jsonl."""

    def __init__(self, session_root: str, program_name: str) -> None:
        self.session_root = session_root
        self.program_name = program_name
        self._dir = Path(session_root) / _STATE_DIR / _EXPERIMENTS_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / f"{program_name}.jsonl"

    @property
    def path(self) -> Path:
        return self._path

    def append_result(self, entry: dict) -> None:
        with open(self._path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def read_log(self) -> list[dict]:
        if not self._path.exists():
            return []
        entries: list[dict] = []
        for line in self._path.read_text().splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
        return entries

    def best_result(self, direction: str = "minimize") -> dict | None:
        entries = [e for e in self.read_log() if e.get("status") == "accepted"]
        if not entries:
            return None
        if direction == "minimize":
            return min(entries, key=lambda e: e.get("metric_after", float("inf")))
        return max(entries, key=lambda e: e.get("metric_after", float("-inf")))

    def summary(self, direction: str = "minimize") -> dict:
        entries = self.read_log()
        accepted = [e for e in entries if e.get("status") == "accepted"]
        rejected = [e for e in entries if e.get("status") == "rejected"]
        errored = [e for e in entries if e.get("status") == "error"]
        total_duration = sum(e.get("duration_s", 0) for e in entries)
        best = self.best_result(direction)
        return {
            "program": self.program_name,
            "total": len(entries),
            "accepted": len(accepted),
            "rejected": len(rejected),
            "errored": len(errored),
            "total_duration_s": total_duration,
            "best": best,
        }


# ---------------------------------------------------------------------------
# Result file parsing
# ---------------------------------------------------------------------------


def _results_dir(session_root: str) -> Path:
    d = Path(session_root) / _STATE_DIR / _EXPERIMENTS_DIR / _RESULTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_result_file(session_root: str, exp_id: str) -> dict | None:
    result_path = _results_dir(session_root) / f"{exp_id}.json"
    if not result_path.exists():
        return None
    try:
        return json.loads(result_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Metric comparison
# ---------------------------------------------------------------------------


def _metric_improved(
    before: float | None, after: float | None, direction: str = "minimize"
) -> bool:
    if before is None or after is None:
        return False
    if direction == "minimize":
        return after < before
    return after > before


# ---------------------------------------------------------------------------
# Single experiment
# ---------------------------------------------------------------------------


def run_experiment(
    project_root: str,
    program_text: str,
    metric_name: str,
    metric_baseline: float | None,
    agent: str = "claude",
    direction: str = "minimize",
    session_root: str | None = None,
    timeout: int = 600,
    exp_id: str | None = None,
) -> dict:
    """Run a single experiment iteration.

    1. Dispatch worker pane with the experiment prompt + baseline info.
    2. Wait for worker to finish.
    3. Parse result file at .dgov/experiments/results/<exp_id>.json.
    4. If metric improves: merge, log as accepted.
    5. If metric regresses or missing: close without merge, log as rejected/error.
    """
    import dgov.panes as _p

    session_root = os.path.abspath(session_root or project_root)
    exp_id = exp_id or f"exp-{uuid.uuid4().hex[:8]}"
    slug = exp_id

    # Build the prompt with baseline context
    results_path = _results_dir(session_root) / f"{exp_id}.json"
    baseline_info = (
        f"\n\nCurrent baseline metric ({metric_name}): {metric_baseline}"
        if metric_baseline is not None
        else f"\n\nMetric to optimize: {metric_name} (no baseline yet)"
    )
    result_instructions = (
        f"\n\nWhen done, write your results to: {results_path}\n"
        f'Format: {{"metric_name": "{metric_name}", "metric_value": <number>, '
        f'"hypothesis": "<what you tried>", "follow_ups": ["<idea1>", "<idea2>"]}}'
    )
    full_prompt = program_text + baseline_info + result_instructions

    _emit_event(session_root, "experiment_started", slug, metric_name=metric_name)

    start_time = time.monotonic()

    # Dispatch worker
    pane = _p.create_worker_pane(
        project_root=project_root,
        prompt=full_prompt,
        agent=agent,
        permission_mode="bypassPermissions",
        slug=slug,
        session_root=session_root,
    )

    # Wait for worker
    try:
        _p.wait_worker_pane(
            project_root,
            pane.slug,
            session_root=session_root,
            timeout=timeout,
        )
    except _p.PaneTimeoutError:
        duration_s = time.monotonic() - start_time
        result = {
            "id": exp_id,
            "hypothesis": program_text[:200],
            "metric_name": metric_name,
            "metric_before": metric_baseline,
            "metric_after": None,
            "status": "error",
            "error": "timeout",
            "agent": agent,
            "duration_s": round(duration_s, 1),
            "follow_ups": [],
            "commit_sha": None,
        }
        _p.close_worker_pane(project_root, slug, session_root=session_root, force=True)
        return result

    duration_s = time.monotonic() - start_time

    # Parse result file
    worker_result = _read_result_file(session_root, exp_id)

    if worker_result is None:
        result = {
            "id": exp_id,
            "hypothesis": program_text[:200],
            "metric_name": metric_name,
            "metric_before": metric_baseline,
            "metric_after": None,
            "status": "error",
            "error": "no_result_file",
            "agent": agent,
            "duration_s": round(duration_s, 1),
            "follow_ups": [],
            "commit_sha": None,
        }
        _p.close_worker_pane(project_root, slug, session_root=session_root, force=True)
        return result

    metric_after = worker_result.get("metric_value")
    hypothesis = worker_result.get("hypothesis", program_text[:200])
    follow_ups = worker_result.get("follow_ups", [])

    improved = _metric_improved(metric_baseline, metric_after, direction)

    if improved:
        merge_result = _p.merge_worker_pane(project_root, slug, session_root=session_root)
        commit_sha = None
        if "merged" in merge_result:
            # Get the merge SHA
            import subprocess

            sha_r = subprocess.run(
                ["git", "-C", project_root, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
            )
            commit_sha = sha_r.stdout.strip() if sha_r.returncode == 0 else None
            status = "accepted"
            _emit_event(session_root, "experiment_accepted", slug, metric_after=metric_after)
        else:
            status = "error"
            _p.close_worker_pane(project_root, slug, session_root=session_root, force=True)
    else:
        status = "rejected"
        commit_sha = None
        _emit_event(session_root, "experiment_rejected", slug, metric_after=metric_after)
        _p.close_worker_pane(project_root, slug, session_root=session_root, force=True)

    return {
        "id": exp_id,
        "hypothesis": hypothesis,
        "metric_name": metric_name,
        "metric_before": metric_baseline,
        "metric_after": metric_after,
        "status": status,
        "agent": agent,
        "duration_s": round(duration_s, 1),
        "follow_ups": follow_ups,
        "commit_sha": commit_sha,
    }


# ---------------------------------------------------------------------------
# Experiment loop
# ---------------------------------------------------------------------------


def run_experiment_loop(
    project_root: str,
    program_path: str,
    metric_name: str,
    budget: int = 5,
    agent: str = "claude",
    direction: str = "minimize",
    session_root: str | None = None,
    timeout: int = 600,
) -> dict:
    """Run an experiment loop up to *budget* times.

    After each experiment, reads follow_ups from the log to pick the next
    hypothesis. If no follow_ups, re-uses the original program.

    Returns a summary dict with all results.
    """
    session_root = os.path.abspath(session_root or project_root)
    program_text = Path(program_path).read_text()
    program_name = Path(program_path).stem

    log = ExperimentLog(session_root, program_name)
    results: list[dict] = []

    # Current baseline: best accepted result so far, or None
    best = log.best_result(direction)
    baseline = best["metric_after"] if best else None

    current_prompt = program_text

    for i in range(budget):
        exp_id = f"exp-{uuid.uuid4().hex[:8]}"

        result = run_experiment(
            project_root=project_root,
            program_text=current_prompt,
            metric_name=metric_name,
            metric_baseline=baseline,
            agent=agent,
            direction=direction,
            session_root=session_root,
            timeout=timeout,
            exp_id=exp_id,
        )

        log.append_result(result)
        results.append(result)

        # Yield progress (for CLI streaming)
        yield result

        # Update baseline if accepted
        if result["status"] == "accepted" and result["metric_after"] is not None:
            baseline = result["metric_after"]

        # Pick next hypothesis from follow_ups
        follow_ups = result.get("follow_ups", [])
        if follow_ups:
            current_prompt = (
                f"Previous experiment: {result.get('hypothesis', '')}\n"
                f"Result: {result['status']} "
                f"(metric {result.get('metric_before')} -> {result.get('metric_after')})\n\n"
                f"Next hypothesis to try: {follow_ups[0]}\n\n"
                f"Original program context:\n{program_text}"
            )
        else:
            current_prompt = program_text

    return log.summary(direction)
