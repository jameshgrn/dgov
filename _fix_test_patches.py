#!/usr/bin/env python3
"""Fix test patches: change dgov.panes.X to source module for removed re-exports."""

from pathlib import Path

# Map of removed re-export symbol → actual source module path for patching
PATCH_REDIRECTS = {
    # waiter
    "_agent_still_running": "dgov.waiter",
    "_has_new_commits": "dgov.waiter",
    "_detect_blocked": "dgov.waiter",
    "_poll_once": "dgov.waiter",
    "interact_with_pane": "dgov.waiter",
    "nudge_pane": "dgov.waiter",
    "signal_pane": "dgov.waiter",
    "wait_all_worker_panes": "dgov.waiter",
    "wait_worker_pane": "dgov.waiter",
    # merger
    "_commit_worktree": "dgov.merger",
    "_detect_conflicts": "dgov.merger",
    "_lint_fix_merged_files": "dgov.merger",
    "_pick_resolver_agent": "dgov.merger",
    "_plumbing_merge": "dgov.merger",
    "_resolve_conflicts_with_agent": "dgov.merger",
    "_restore_protected_files": "dgov.merger",
    "merge_worker_pane": "dgov.merger",
    "merge_worker_pane_with_close": "dgov.merger",
    # openrouter
    "_QWEN_4B_TIMEOUT": "dgov.openrouter",
    "_QWEN_4B_URL": "dgov.openrouter",
    "_qwen_4b_request": "dgov.openrouter",
    "chat_completion": "dgov.openrouter",
    # responder
    "BUILT_IN_RULES": "dgov.responder",
    "COOLDOWN_SECONDS": "dgov.responder",
    "ResponseRule": "dgov.responder",
    "auto_respond": "dgov.responder",
    "check_cooldown": "dgov.responder",
    "load_response_rules": "dgov.responder",
    "match_response": "dgov.responder",
    "record_cooldown": "dgov.responder",
    "reset_cooldowns": "dgov.responder",
    # retry
    "RetryPolicy": "dgov.retry",
    "get_retry_policy": "dgov.retry",
    "maybe_auto_retry": "dgov.retry",
    "retry_context": "dgov.retry",
    # review_fix
    "ReviewFinding": "dgov.review_fix",
    "parse_review_findings": "dgov.review_fix",
    "run_review_fix_pipeline": "dgov.review_fix",
    # strategy
    "_SLUG_RE": "dgov.strategy",
    "classify_task": "dgov.strategy",
    "_validate_slug": "dgov.strategy",
    # templates
    "BUILT_IN_TEMPLATES": "dgov.templates",
    "PromptTemplate": "dgov.templates",
    "list_templates": "dgov.templates",
    "load_templates": "dgov.templates",
    "render_template": "dgov.templates",
    # persistence
    "PANE_STATES": "dgov.persistence",
    "VALID_EVENTS": "dgov.persistence",
    "VALID_TRANSITIONS": "dgov.persistence",
    "IllegalTransitionError": "dgov.persistence",
    "_state_path": "dgov.persistence",
    "_validate_state": "dgov.persistence",
    # batch
    "_compute_tiers": "dgov.batch",
    "create_checkpoint": "dgov.batch",
    "list_checkpoints": "dgov.batch",
    "run_batch": "dgov.batch",
    # experiment
    "ExperimentLog": "dgov.experiment",
    "run_experiment": "dgov.experiment",
    "run_experiment_loop": "dgov.experiment",
}


def process_file(path: Path) -> int:
    """Process a file, return number of replacements made."""
    content = path.read_text()
    count = 0
    lines = content.split("\n")
    new_lines = []

    for line in lines:
        new_line = line
        for sym, target_mod in PATCH_REDIRECTS.items():
            # Match dgov.panes.SYMBOL followed by quote, comma, paren, etc.
            # Handles: patch("dgov.panes.X"), monkeypatch.setattr("dgov.panes.X", ...)
            for quote in ['"', "'"]:
                old = f"dgov.panes.{sym}{quote}"
                new = f"{target_mod}.{sym}{quote}"
                if old in new_line:
                    new_line = new_line.replace(old, new)
                    count += 1
        new_lines.append(new_line)

    if count > 0:
        path.write_text("\n".join(new_lines))
    return count


def main():
    root = Path("/Users/jakegearon/projects/dgov/.dgov/worktrees/split-barrel")
    total = 0
    for p in sorted((root / "tests").rglob("*.py")):
        c = process_file(p)
        if c:
            print(f"  {p.relative_to(root)}: {c} replacements")
            total += c
    print(f"\nTotal: {total} replacements")


if __name__ == "__main__":
    main()
