# CODEBASE

## ROUTING
pane lifecycle: lifecycle.py + persistence.py done.py gitops.py
merge/review: merger.py + inspection.py persistence.py
review diffs: inspection.py + merger.py
retry/escalation: recovery.py + responder.py monitor.py
monitor daemon: monitor.py + monitor_hooks.py recovery.py
done detection: done.py waiter.py + lifecycle.py
agent routing: router.py agents.py + strategy.py
decisions: decision.py decision_providers.py + provider_registry.py
templates: templates.py strategy.py + lifecycle.py
dashboard TUI: dashboard.py terrain.py + terrain_pane.py
DAG/batch/plan: dag.py batch.py plan.py + dag_parser.py dag_graph.py kernel.py
state DB: persistence.py + status.py
CLI: cli/admin.py cli/pane.py + cli/__init__.py

## INVARIANTS
- git worktree, not main repo. no merge/rebase/pull.
- CLAUDE.md git-excluded. read-only, cannot commit.
- dgov worker complete auto-commits unstaged changes.
- protected files restored at merge. changes discarded.
- no push to remote. no full test suite.

## MODULES
[orchestration core]
  src/dgov/done.py (L): Done-signal and done-detection helpers.
  src/dgov/executor.py (L): Shared executor policy for dispatch preflight and merge review gates. -> test_executor.py
  src/dgov/gitops.py (S): Low-level git plumbing helpers for worktree and branch management. -> test_gitops.py
  src/dgov/kernel.py (L): Deterministic kernel primitives for pane and DAG lifecycle. -> test_kernel.py
  src/dgov/lifecycle.py (L): Pane lifecycle: create, close, resume, and cleanup. -> test_lifecycle.py
  src/dgov/persistence.py (L): State file management and event journal.
  src/dgov/status.py (L): Pane status: list, freshness, output capture, pruning. -> test_status.py
  src/dgov/waiter.py (L): Wait/poll logic for worker panes. -> test_waiter.py
[merge and review]
  src/dgov/inspection.py (L): Pane inspection: review, diff, rebase. -> test_inspection.py
  src/dgov/merger.py (L): Git merge, conflict resolution, and post-merge operations.
[automation and recovery]
  src/dgov/monitor.py (L): Lightweight polling daemon for worker state classification and auto-remediation. -> test_monitor.py
  src/dgov/monitor_hooks.py (S): Configurable monitor hooks via TOML configuration files. -> test_monitor_hooks.py
  src/dgov/recovery.py (L): Pane recovery: retry policy, escalation, and bounded retry with auto-escalation.
  src/dgov/responder.py (S): Auto-respond to blocked worker panes. -> test_responder.py
[agent integration]
  src/dgov/agents.py (L): Agent registry and launch command builder.
  src/dgov/cli/templates.py (S): Prompt template commands. -> test_templates.py
  src/dgov/openrouter.py (M): OpenRouter API client with local Qwen 4B fallback. -> test_openrouter.py
  src/dgov/router.py (M): Agent router: resolve logical model names to available physical backends. -> test_router.py
  src/dgov/strategy.py (M): Task routing, slug generation, and prompt structuring. -> test_strategy.py
  src/dgov/templates.py (S): Prompt template system for worker panes. -> test_templates.py
[decision system]
  src/dgov/context_packet.py (S): Compiled task context shared across preflight, prompts, and instructions. -> test_context_packet.py
  src/dgov/decision.py (L): Typed decision requests, records, and provider wrappers. -> test_decision.py
  src/dgov/decision_providers.py (S): # Error parsing: invalid syntax (decision_providers.py, line 443) -> test_decision_providers.py
  src/dgov/provider_registry.py (S): Central provider selection and optional decision journaling. -> test_provider_registry.py
[higher-level workflows]
  src/dgov/batch.py (M): Batch execution and checkpoint management. -> test_batch.py
  src/dgov/dag.py (M): DAG file parser and execution engine for dgov. -> test_dag.py
  src/dgov/dag_graph.py (S): DAG graph algorithms: validation, topological sort, tier computation. -> test_dag_graph.py
  src/dgov/dag_parser.py (S): DAG file dataclasses and TOML parser. -> test_dag_parser.py
  src/dgov/review_fix.py (M): Review-then-fix pipeline: dispatch review workers, parse findings, dispatch fix workers. -> test_review_fix.py
[other]
  src/dgov/api.py (M): Public Python API for dgov.
  src/dgov/backend.py (M): Abstract worker backend interface and tmux implementation. -> test_backend.py
  src/dgov/blame.py (M): Blame: query event journal + git history to attribute file changes to agents. -> test_blame.py
  src/dgov/cli/admin.py (L): Administrative and diagnostic commands.
  src/dgov/cli/batch_cmd.py (S): Checkpoint and batch commands.
  src/dgov/cli/briefing_cmd.py (S): CLI command: dgov briefing — on-demand document viewer via glow.
  src/dgov/cli/dag_cmd.py (M): CLI commands for DAG execution.
  src/dgov/cli/journal_cmd.py (S): Decision journal query CLI.
  src/dgov/cli/ledger_cmd.py (S): Operational ledger CLI — formalized napkin.
  src/dgov/cli/merge_queue_cmd.py (S): Merge queue commands for governor-side queue processing.
  src/dgov/cli/monitor_cmd.py (S): Monitor daemon CLI command.
  src/dgov/cli/openrouter_cmd.py (S): OpenRouter integration commands.
  src/dgov/cli/pane.py (L): Pane management commands.
  src/dgov/cli/plan_cmd.py (M): CLI commands for dgov plan execution.
  src/dgov/cli/review_fix_cmd.py (S): Review-fix pipeline command.
  src/dgov/cli/trace_cmd.py (M): Span and tool-trace CLI commands.
  src/dgov/cli/wait_cmd.py (S): CLI command for event-driven waiting and governor interrupts.
  src/dgov/cli/worker_cmd.py (S): Worker status reporting commands.
  src/dgov/dashboard.py (L): Rich-based live dashboard for dgov pane management. -> test_dashboard.py
  src/dgov/plan.py (L): Plan schema, validator, and compiler for dgov. -> test_plan.py
  src/dgov/preflight.py (L): Pre-flight validation for dgov dispatch.
  src/dgov/spans.py (L): Structured span and tool-trace observability for dgov. -> test_spans.py
  src/dgov/terrain.py (L): SPIM erosion terrain model for dgov dashboard.
  src/dgov/terrain_pane.py (M): Standalone terrain simulation pane for dgov governor workspace. -> test_terrain_pane.py
  src/dgov/tmux.py (L): Thin wrappers around tmux commands.

## CALL GRAPH
create_worker_pane:
  load_registry + resolve_agent
  get_backend.create_worker_pane (tmux)
  add_pane (state.db)
  _write_worktree_instructions (context)
  _wrap_done_signal (exit detection)
merge_worker_pane:
  _restore_protected_files
  _commit_worktree (auto-commit)
  _rebase_onto_head
  _plumbing_merge (in-memory)
  _full_cleanup (kill + remove)
  _lint_fix_merged_files (ruff)
  _run_related_tests (pytest)

## STATES
active -> done -> merged -> removed
active -> timed_out -> retry -> active
active -> failed -> retry -> active
active -> abandoned -> closed
any terminal -> closed

## CLI REGISTRATION
pane sub: @pane.command("name") in cli/pane.py
top-level: fn in cli/*.py, import+add_command in cli/__init__.py

## TESTS
src/dgov/agents.py -> test_dgov_agents.py test_dgov_preflight.py test_circuit_breaker.py +2
src/dgov/backend.py -> test_backend.py test_cascade_close.py test_circuit_breaker.py +4
src/dgov/batch.py -> test_batch.py test_batch_dag.py
src/dgov/blame.py -> test_blame.py test_dgov_blame.py
src/dgov/cli/__init__.py -> test_dgov_cli.py test_cli_admin.py test_dgov_state.py +1
src/dgov/cli/admin.py -> test_cli_admin.py test_dgov_cli.py test_init_doctor.py
src/dgov/cli/pane.py -> test_dgov_cli.py test_cli_pane.py test_comms.py +1
src/dgov/cli/templates.py -> test_templates.py
src/dgov/context_packet.py -> test_context_packet.py
src/dgov/dag.py -> test_dag.py
src/dgov/dag_graph.py -> test_dag_graph.py test_dag_internals.py test_dag.py
src/dgov/dag_parser.py -> test_dag_parser.py test_dag_graph.py test_dag_internals.py +1
src/dgov/dashboard.py -> test_dashboard.py
src/dgov/decision.py -> test_decision.py test_decision_providers.py test_monitor.py +1
src/dgov/decision_providers.py -> test_decision_providers.py test_decision.py test_provider_registry.py
src/dgov/done.py -> test_done_strategy.py test_circuit_breaker.py test_comms.py +1
src/dgov/executor.py -> test_executor.py test_kernel.py test_cli_pane.py +1
src/dgov/gitops.py -> test_gitops.py
src/dgov/inspection.py -> test_inspection.py test_dgov_helpers.py test_integration.py +1
src/dgov/kernel.py -> test_kernel.py
src/dgov/lifecycle.py -> test_lifecycle.py test_executor.py test_dgov_helpers.py +2
src/dgov/merger.py -> test_dgov_merger.py test_merger_conflicts.py test_merger_coverage.py +2
src/dgov/monitor.py -> test_monitor.py
src/dgov/monitor_hooks.py -> test_monitor_hooks.py
src/dgov/openrouter.py -> test_openrouter.py
src/dgov/persistence.py -> test_persistence_pane.py test_dgov_state.py test_transitions.py +15
src/dgov/plan.py -> test_plan.py
src/dgov/preflight.py -> test_dgov_preflight.py
src/dgov/provider_registry.py -> test_provider_registry.py
src/dgov/recovery.py -> test_bounded_retry.py test_recovery_dogfood.py test_retry.py
src/dgov/responder.py -> test_responder.py
src/dgov/review_fix.py -> test_review_fix.py
src/dgov/router.py -> test_router.py
src/dgov/spans.py -> test_spans.py
src/dgov/status.py -> test_status.py test_preview.py test_dgov_panes.py
src/dgov/strategy.py -> test_strategy.py test_dgov_panes.py
src/dgov/templates.py -> test_templates.py
src/dgov/terrain.py -> test_terrain_events.py
src/dgov/terrain_pane.py -> test_terrain_pane.py
src/dgov/tmux.py -> test_dgov_tmux.py
src/dgov/waiter.py -> test_waiter.py test_comms.py
