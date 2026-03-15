"""Pane management — re-exports from sub-modules.

Split into lifecycle, status, inspection, recovery.
All public symbols re-exported here for backward compatibility.
"""

from dgov.inspection import (  # noqa: F401
    diff_worker_pane,
    rebase_governor,
    review_worker_pane,
)
from dgov.lifecycle import (  # noqa: F401
    _build_pane_title,
    _create_worktree,
    _full_cleanup,
    _remove_worktree,
    _trigger_hook,
    close_worker_pane,
    create_worker_pane,
    resume_worker_pane,
)
from dgov.recovery import (  # noqa: F401
    escalate_worker_pane,
    retry_worker_pane,
)
from dgov.status import (  # noqa: F401
    _compute_freshness,
    _count_active_agent_workers,
    capture_worker_output,
    list_worker_panes,
    prune_stale_panes,
)
from dgov.waiter import _is_done, _wrap_done_signal  # noqa: F401
