"""Optional DAG integration — only needed for batch mode.

If distributary is installed, delegates to distributary.dag.get_tiers.
Otherwise, provides a simple fallback that runs all tasks in a single tier.
"""

from __future__ import annotations


def get_tiers(task_specs: list) -> list[list]:
    """Topological sort with touch-conflict serialization.

    Falls back to single-tier (sequential) if distributary is not installed.
    """
    try:
        from distributary.dag import get_tiers as _get_tiers

        return _get_tiers(task_specs)
    except ImportError:
        # Without the DAG engine, run everything sequentially
        return [task_specs] if task_specs else []
