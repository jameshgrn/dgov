"""DAG graph algorithms: validation, topological sort, tier computation."""

from __future__ import annotations


def _normalize_touch_path(path: str) -> str:
    """Normalize a file path for comparison."""
    return path.strip().lstrip("./").rstrip("/")


def _paths_overlap(path: str, touch: str) -> bool:
    """Check if two paths overlap (identical or one is a parent of other)."""
    norm_path = _normalize_touch_path(path)
    norm_touch = _normalize_touch_path(touch)
    if not norm_path or not norm_touch:
        return False
    return (
        norm_path == norm_touch
        or norm_path.startswith(norm_touch + "/")
        or norm_touch.startswith(norm_path + "/")
    )


def compute_tiers(deps: dict[str, tuple[str, ...]]) -> list[list[str]]:
    """Compute execution tiers from a dependency map."""
    if not deps:
        return []

    tiers = []
    remaining = set(deps.keys())
    completed = set()

    while remaining:
        current_tier = []
        for slug in sorted(remaining):
            if all(d in completed for d in deps[slug]):
                current_tier.append(slug)

        if not current_tier:
            # Cycle detected or missing dependency
            break

        tiers.append(current_tier)
        completed.update(current_tier)
        remaining.difference_update(current_tier)

    return tiers
