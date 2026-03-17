"""SQLite-backed region locks for coordinating SPIM agent work."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import models


class RegionLockManager:
    def __init__(
        self,
        db_path: str | Path,
        *,
        now_fn: Callable[[], datetime] = models.utc_now,
    ) -> None:
        self.db_path = Path(db_path)
        self._now_fn = now_fn
        models.ensure_schema(self.db_path)

    def acquire(self, region: str, agent_id: int, ttl: float) -> bool:
        expires_at = models.add_ttl(self._now_fn(), ttl)
        now_iso = models.isoformat_utc(self._now_fn())

        def _do() -> bool:
            conn = models.ensure_schema(self.db_path)
            cursor = conn.execute(
                """
                INSERT INTO locks (region, held_by, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(region) DO UPDATE SET
                    held_by = excluded.held_by,
                    expires_at = excluded.expires_at
                WHERE locks.held_by = excluded.held_by OR locks.expires_at <= ?
                """,
                (region, agent_id, expires_at, now_iso),
            )
            conn.commit()
            return cursor.rowcount > 0

        return bool(models._retry_on_lock(_do))

    def release(self, region: str, agent_id: int) -> bool:
        return models.delete_lock(self.db_path, region, agent_id)

    def check(self, region: str) -> dict[str, Any] | None:
        lock = models.get_lock(self.db_path, region)
        if lock is None:
            return None
        if str(lock["expires_at"]) <= models.isoformat_utc(self._now_fn()):
            models.delete_lock(self.db_path, region)
            return None
        return lock

    def expire_stale(self) -> int:
        now_iso = models.isoformat_utc(self._now_fn())

        def _do() -> int:
            conn = models.ensure_schema(self.db_path)
            cursor = conn.execute("DELETE FROM locks WHERE expires_at <= ?", (now_iso,))
            conn.commit()
            return int(cursor.rowcount)

        return int(models._retry_on_lock(_do))
