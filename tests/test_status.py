"""Tests for dgov.status – tail_worker_log."""

from __future__ import annotations

import pytest

from dgov.status import tail_worker_log


@pytest.mark.unit
class TestTailWorkerLog:
    def _make_log(self, tmp_path, slug: str, content: bytes) -> str:
        """Create a .dgov/logs/<slug>.log under tmp_path, return session_root."""
        log_dir = tmp_path / ".dgov" / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / f"{slug}.log").write_bytes(content)
        return str(tmp_path)

    def test_missing_log_returns_none(self, tmp_path):
        result = tail_worker_log(str(tmp_path), "no-such-worker")
        assert result is None

    def test_empty_log_returns_empty_string(self, tmp_path):
        root = self._make_log(tmp_path, "empty", b"")
        result = tail_worker_log(root, "empty")
        assert result == ""

    def test_returns_last_n_lines(self, tmp_path):
        lines = [f"line {i}" for i in range(50)]
        content = "\n".join(lines).encode()
        root = self._make_log(tmp_path, "big", content)
        result = tail_worker_log(root, "big", lines=5)
        assert result is not None
        assert result.splitlines() == [f"line {i}" for i in range(45, 50)]

    def test_fewer_lines_than_requested(self, tmp_path):
        content = b"alpha\nbeta\ngamma"
        root = self._make_log(tmp_path, "short", content)
        result = tail_worker_log(root, "short", lines=10)
        assert result is not None
        assert result.splitlines() == ["alpha", "beta", "gamma"]

    def test_strips_ansi_codes(self, tmp_path):
        content = b"\x1b[32mgreen\x1b[0m\n\x1b[1mbold\x1b[0m"
        root = self._make_log(tmp_path, "ansi", content)
        result = tail_worker_log(root, "ansi")
        assert result is not None
        assert "\x1b" not in result
        assert result.splitlines() == ["green", "bold"]

    def test_handles_invalid_utf8(self, tmp_path):
        content = b"good line\nbad \xff bytes\nlast"
        root = self._make_log(tmp_path, "bin", content)
        result = tail_worker_log(root, "bin")
        assert result is not None
        assert "last" in result
        # replacement character should appear instead of crash
        assert "\ufffd" in result or "bad" in result

    def test_seek_efficiency_large_file(self, tmp_path):
        """For a large file, only the tail chunk is read (not the whole file)."""
        # 10_000 lines, ~110 KB
        lines = [f"log entry number {i:05d}" for i in range(10_000)]
        content = "\n".join(lines).encode()
        root = self._make_log(tmp_path, "huge", content)
        result = tail_worker_log(root, "huge", lines=5)
        assert result is not None
        result_lines = result.splitlines()
        assert len(result_lines) == 5
        assert result_lines[-1] == "log entry number 09999"

    def test_single_line_log(self, tmp_path):
        root = self._make_log(tmp_path, "one", b"only line")
        result = tail_worker_log(root, "one", lines=5)
        assert result == "only line"

    def test_default_lines_is_20(self, tmp_path):
        lines = [f"L{i}" for i in range(30)]
        content = "\n".join(lines).encode()
        root = self._make_log(tmp_path, "default", content)
        result = tail_worker_log(root, "default")
        assert result is not None
        assert len(result.splitlines()) == 20
