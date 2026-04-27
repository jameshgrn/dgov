"""Prompt construction for dgov workers.

Owns all prompt assembly: worker prompts, reviewer prompts, self-review
prompts, fork handoff prompts, and settlement retry prompts. Pure
string-in/string-out — no async, no state machine interaction.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from dgov.dag_parser import DagDefinition, DagTaskSpec

logger = logging.getLogger(__name__)

_REVIEW_APPLIES_TO = frozenset({"review", "reviewer"})


def build_baseline_diag_note(config: object, session_root: str) -> str:
    """Capture baseline type-check diagnostic count for worker context.

    Returns a short note string to prepend to worker prompts, or empty
    string if no type checker is configured or baseline is clean.
    """
    from dgov.settlement import _count_diagnostics

    type_check_cmd = getattr(config, "type_check_cmd", None)
    if not type_check_cmd:
        return ""
    try:
        res = subprocess.run(
            type_check_cmd,
            shell=True,
            cwd=session_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return ""
    if res.returncode == 0:
        return ""
    count = _count_diagnostics((res.stdout or "") + (res.stderr or ""))
    if count == 0:
        return ""
    return (
        f"\nNOTE: The type checker (`{type_check_cmd}`) has {count} pre-existing "
        f"diagnostic(s) at HEAD. These are NOT your responsibility — do not "
        f"attempt to fix them. Settlement compares against this baseline and "
        f"will only reject if you introduce NEW diagnostics.\n"
    )


def load_review_sop_blocks(session_root: str) -> tuple[str, ...]:
    """Load SOPs tagged for review and render their prompt blocks.

    Called once at runner init. Returns pre-rendered blocks for injection
    into self-review prompts. Falls back to empty tuple if SOPs dir is
    missing or unparseable (self-review still works, just without SOP
    guidance).
    """
    from dgov.sop_bundler import load_sops

    sops_dir = Path(session_root) / ".dgov" / "sops"
    try:
        all_sops = load_sops(sops_dir)
    except (ValueError, OSError) as exc:
        logger.warning("Failed to load review SOPs from %s: %s", sops_dir, exc)
        return ()
    review_sops = [s for s in all_sops if _REVIEW_APPLIES_TO & frozenset(s.applies_to)]
    return tuple(s.render_prompt_block() for s in review_sops)


class PromptBuilder:
    """Assembles all prompts for dgov worker dispatch.

    Instantiated once per runner with cached baseline/SOP data.
    Methods are pure string builders — no I/O except ledger queries.
    """

    def __init__(
        self,
        session_root: str,
        dag: DagDefinition,
        baseline_diag_note: str,
        review_sop_blocks: tuple[str, ...],
    ) -> None:
        self.session_root = session_root
        self.dag = dag
        self._baseline_diag_note = baseline_diag_note
        self._review_sop_blocks = review_sop_blocks

    def worker_prompt(
        self,
        task_slug: str,
        task: DagTaskSpec,
        prior_error: str | None = None,
        attempt: int = 0,
    ) -> str:
        """Build the full worker prompt with all enrichments.

        This includes:
        1. Base prompt (or reviewer-generated prompt for reviewer role)
        2. Baseline diagnostic note
        3. Active probation (case law) from ledger entries
        4. Prior error context for retries
        """
        # Build base prompt
        if task.role == "reviewer":
            prompt = self.reviewer_prompt(task_slug, task)
        else:
            prompt = task.prompt or ""

        # Inject baseline diagnostic note so workers don't waste iterations
        if self._baseline_diag_note:
            prompt = self._baseline_diag_note + prompt

        # Inject active probation (case law) from ledger
        ledger_entries = self._get_ledger_entries(task)
        probation_section = self._format_probation_section(ledger_entries)
        if probation_section:
            prompt = prompt + probation_section

        # Enrich prompt with prior failure context on retry
        if prior_error:
            prompt = (
                f"PREVIOUS ATTEMPT ({attempt}) FAILED:\n{prior_error}\n\n"
                f"Fix the issue described above, then complete the original task.\n\n"
                f"ORIGINAL TASK:\n{prompt}"
            )

        return prompt

    def reviewer_prompt(self, task_slug: str, task: DagTaskSpec) -> str:
        """Build a reviewer prompt with dependency diffs auto-injected."""
        import subprocess as sp

        from dgov import deploy_log

        sections: list[str] = []
        sections.append(
            "Review the following changes for semantic correctness.\n"
            "Focus on: logic errors, no-ops, silently wrong behavior, "
            "missing edge cases, and whether the code matches its stated intent.\n"
        )

        records = deploy_log.read(self.session_root, self.dag.name)
        sha_by_unit = {r.unit: r.sha for r in records}

        for dep_slug in task.depends_on:
            dep_task = self.dag.tasks.get(dep_slug)
            if not dep_task:
                continue
            sha = sha_by_unit.get(dep_slug)
            if not sha:
                sections.append(f"## {dep_slug}\nNo deploy record found (not yet merged).\n")
                continue

            diff_result = sp.run(
                ["git", "show", "--stat", "--patch", sha],
                cwd=self.session_root,
                capture_output=True,
                text=True,
            )
            diff_text = diff_result.stdout if diff_result.returncode == 0 else "(diff unavailable)"

            sections.append(
                f"## Task: {dep_slug}\n"
                f"Summary: {dep_task.summary}\n"
                f"Commit: {dep_task.commit_message}\n\n"
                f"```diff\n{diff_text}\n```\n"
            )

        # Append user-provided prompt guidance if any
        if task.prompt and task.prompt.strip():
            sections.append(f"## Additional review guidance\n{task.prompt}\n")

        sections.append(
            "Respond via the `done` tool with your verdict as a JSON object:\n"
            '{"approved": true/false, "issues": ["issue 1", ...]}\n'
            'If approved with no issues, use: {"approved": true, "issues": []}'
        )

        return "\n".join(sections)

    def self_review_prompt(self, diff_text: str) -> str:
        """Build a clean-context review prompt.

        Framing and verdict protocol live here (code). Review criteria come
        from SOPs loaded at runner init (policy). No task prompt, no worker
        reasoning — the reviewer sees only the diff and SOP guidance.
        """
        parts = [
            "You are reviewing a code change for semantic correctness.\n"
            "You have NO context about what the change was supposed to do.\n"
            "Reason backward from the implementation itself.\n",
        ]
        if self._review_sop_blocks:
            parts.append("\n" + "\n\n".join(self._review_sop_blocks) + "\n")
        else:
            parts.append(
                "\nFocus on:\n"
                "- Logic errors (wrong conditions, off-by-one, inverted checks)\n"
                "- No-ops (code that appears to do something but has no effect)\n"
                "- Silently wrong behavior (swallowed errors, wrong variable used)\n"
                "- Missing edge cases (null/empty/boundary conditions)\n"
            )
        parts.append(
            f"\nDIFF:\n```diff\n{diff_text}\n```\n\n"
            "Read the surrounding code in the repo for context if needed.\n\n"
            "Respond via the `done` tool with a JSON verdict:\n"
            '{"approved": true, "issues": []}  -- if no semantic issues\n'
            '{"approved": false, "issues": ["description of issue 1", '
            '"description of issue 2"]}  -- if issues found\n'
        )
        return "".join(parts)

    @staticmethod
    def fork_handoff_prompt(task: DagTaskSpec, diff_text: str) -> str:
        """Build a clean-context handoff prompt for a forked worker."""
        return (
            "You are continuing work that a previous worker started but did not "
            "complete. The previous worker ran out of iterations. Their changes "
            "are already in the worktree.\n\n"
            "YOUR TASK (original):\n"
            f"{task.prompt or ''}\n\n"
            "CHANGES MADE SO FAR:\n"
            f"```diff\n{diff_text}\n```\n\n"
            "INSTRUCTIONS:\n"
            "1. Use git_diff to see the current state of all changes.\n"
            "2. Complete any remaining work for the task.\n"
            "3. If the changes look complete, verify them (check_syntax, "
            "run_tests) and call done.\n"
            "4. If the changes are incomplete or incorrect, fix them and call "
            "done.\n"
            "Do NOT start from scratch. Build on the existing work.\n"
        )

    @staticmethod
    def settlement_retry_prompt(task: DagTaskSpec, settlement_error: str) -> str:
        """Build retry prompt after settlement rejection."""
        return (
            "Your previous attempt was REJECTED by settlement. "
            "Fix the issue and call done.\n\n"
            f"SETTLEMENT ERROR:\n{settlement_error}\n\n"
            f"ORIGINAL TASK:\n{task.prompt or ''}\n\n"
            "The worktree has your changes (uncommitted). "
            "Use git_diff to see them, fix the problem, then call done."
        )

    def _get_ledger_entries(self, task: DagTaskSpec) -> list[dict]:
        """Query ledger for open entries where affected_paths intersects with task claims."""
        from dgov.persistence.ledger import list_ledger_entries

        def _normalize(path: str) -> str:
            return path.strip().lstrip("./").rstrip("/")

        def _overlaps(left: str, right: str) -> bool:
            left_norm = _normalize(left)
            right_norm = _normalize(right)
            if not left_norm or not right_norm:
                return False
            return (
                left_norm == right_norm
                or left_norm.startswith(right_norm + "/")
                or right_norm.startswith(left_norm + "/")
            )

        # Get all file paths this task claims (create, edit, delete, touch)
        task_paths = {_normalize(path) for path in task.all_touches() if _normalize(path)}
        if not task_paths:
            return []

        # Query for open ledger entries
        entries = list_ledger_entries(self.session_root, status="open")

        # Filter to entries with path overlap
        overlapping = []
        for entry in entries:
            entry_paths = {_normalize(path) for path in entry.affected_paths if _normalize(path)}
            if any(
                _overlaps(entry_path, task_path)
                for entry_path in entry_paths
                for task_path in task_paths
            ):
                overlapping.append({"id": entry.id, "content": entry.content})

        return overlapping

    @staticmethod
    def _format_probation_section(entries: list[dict]) -> str:
        """Format ledger entries as Active Probation (Case Law) section."""
        if not entries:
            return ""

        lines = ["\n## Active Probation (Case Law)\n"]
        for entry in entries:
            lines.append(f"**Entry #{entry['id']}:** {entry['content']}\n")

        return "".join(lines)
