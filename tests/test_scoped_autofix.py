"""Tests for scoped autofix — ensuring settlement only modifies worker-changed regions."""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

from dgov.settlement import (
    SmartFixer,
    _expand_to_import_blocks,
    _file_in_base,
    _scope_to_changed,
    _worker_changed_lines,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
        check=True,
    )


def _init_repo(path: Path) -> str:
    _git(path, "init", "-b", "main")
    (path / "README.md").write_text("# test\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "init")
    return _git(path, "rev-parse", "HEAD").stdout.strip()


# ---------------------------------------------------------------------------
# _worker_changed_lines
# ---------------------------------------------------------------------------


class TestWorkerChangedLines:
    def test_single_line_change(self, tmp_path: Path):
        _init_repo(tmp_path)
        (tmp_path / "foo.py").write_text("a = 1\nb = 2\nc = 3\n")
        _git(tmp_path, "add", "foo.py")
        _git(tmp_path, "commit", "-m", "add foo")

        # Worker changes line 2 (0-indexed: line 1)
        (tmp_path / "foo.py").write_text("a = 1\nb = 99\nc = 3\n")

        changed = _worker_changed_lines(tmp_path, "foo.py")
        assert changed == {1}  # 0-indexed

    def test_added_lines(self, tmp_path: Path):
        _init_repo(tmp_path)
        (tmp_path / "foo.py").write_text("a = 1\nc = 3\n")
        _git(tmp_path, "add", "foo.py")
        _git(tmp_path, "commit", "-m", "add foo")

        # Worker inserts a line between a and c
        (tmp_path / "foo.py").write_text("a = 1\nb = 2\nc = 3\n")

        changed = _worker_changed_lines(tmp_path, "foo.py")
        assert 1 in changed  # the inserted line

    def test_no_changes(self, tmp_path: Path):
        _init_repo(tmp_path)
        (tmp_path / "foo.py").write_text("a = 1\n")
        _git(tmp_path, "add", "foo.py")
        _git(tmp_path, "commit", "-m", "add foo")

        changed = _worker_changed_lines(tmp_path, "foo.py")
        assert changed == set()

    def test_new_file(self, tmp_path: Path):
        _init_repo(tmp_path)
        (tmp_path / "new.py").write_text("x = 1\ny = 2\n")

        changed = _worker_changed_lines(tmp_path, "new.py")
        # New untracked files don't show in git diff
        assert changed == set()


# ---------------------------------------------------------------------------
# _scope_to_changed
# ---------------------------------------------------------------------------


class TestScopeToChanged:
    def test_keeps_autofix_in_changed_region(self):
        worker = ["a = 1\n", "b=2\n", "c = 3\n"]
        fixed = ["a = 1\n", "b = 2\n", "c = 3\n"]
        changed = {1}  # worker changed line 1 (0-indexed)

        result = _scope_to_changed(worker, fixed, changed)
        assert result == ["a = 1\n", "b = 2\n", "c = 3\n"]

    def test_reverts_autofix_outside_changed_region(self):
        worker = ["a=1\n", "b = 2\n", "c=3\n"]
        fixed = ["a = 1\n", "b = 2\n", "c = 3\n"]
        changed = {1}  # worker only changed line 1

        result = _scope_to_changed(worker, fixed, changed)
        # line 0 (a=1) should be reverted to worker's version (not in changed)
        # line 1 (b = 2) is unchanged between worker and fixed
        # line 2 (c=3) should be reverted to worker's version (not in changed)
        assert result == ["a=1\n", "b = 2\n", "c=3\n"]

    def test_empty_changed_set_reverts_everything(self):
        worker = ["a=1\n", "b=2\n"]
        fixed = ["a = 1\n", "b = 2\n"]
        changed: set[int] = set()

        result = _scope_to_changed(worker, fixed, changed)
        assert result == worker

    def test_import_block_kept_when_worker_touched_import(self):
        """When worker adds an import, ruff may reorder the whole import block.

        _expand_to_import_blocks widens changed_lines to cover the entire
        import section, so SequenceMatcher's insert/delete ops for the
        reorder are all kept.
        """
        worker = [
            "import os\n",
            "import sys\n",
            "import json\n",  # worker added this
            "\n",
            "x = 1\n",
        ]
        fixed = [
            "import json\n",  # ruff sorted
            "import os\n",
            "import sys\n",
            "\n",
            "x = 1\n",
        ]
        changed = {2}  # worker added line 2

        # Expand to cover the entire import block
        expanded = _expand_to_import_blocks(worker, changed)
        assert expanded == {0, 1, 2}

        result = _scope_to_changed(worker, fixed, expanded)
        # The entire import block should use the fixed (sorted) version
        assert result[0] == "import json\n"
        assert result[1] == "import os\n"
        assert result[2] == "import sys\n"


# ---------------------------------------------------------------------------
# _file_in_base
# ---------------------------------------------------------------------------


class TestFileInBase:
    def test_committed_file(self, tmp_path: Path):
        _init_repo(tmp_path)
        (tmp_path / "foo.py").write_text("x = 1\n")
        _git(tmp_path, "add", "foo.py")
        _git(tmp_path, "commit", "-m", "add foo")

        assert _file_in_base(tmp_path, "foo.py") is True

    def test_new_untracked_file(self, tmp_path: Path):
        _init_repo(tmp_path)
        (tmp_path / "new.py").write_text("x = 1\n")

        assert _file_in_base(tmp_path, "new.py") is False

    def test_nonexistent_file(self, tmp_path: Path):
        _init_repo(tmp_path)
        assert _file_in_base(tmp_path, "nope.py") is False


# ---------------------------------------------------------------------------
# SmartFixer._fix_b904 — text preservation
# ---------------------------------------------------------------------------


class TestB904TextPreservation:
    def test_preserves_double_quotes(self, tmp_path: Path):
        """The old ast.unparse() would convert double quotes to single quotes.
        The new text-level fix must preserve the original quoting.
        """
        content = textwrap.dedent("""\
            def foo():
                try:
                    pass
                except Exception as exc:
                    raise RuntimeError("wrapped")
        """)
        path = tmp_path / "test.py"
        path.write_text(content)

        sf = SmartFixer(tmp_path)
        sf.fix_all(["test.py"])
        fixed = path.read_text()

        assert 'raise RuntimeError("wrapped") from exc' in fixed

    def test_preserves_surrounding_code(self, tmp_path: Path):
        """The fix should only insert ' from exc', not rewrite anything else."""
        content = textwrap.dedent("""\
            # This is a header comment
            import os

            def foo():
                try:
                    os.remove("file.txt")
                except OSError as e:
                    raise ValueError("cleanup failed")

            class Bar:
                pass
        """)
        path = tmp_path / "test.py"
        path.write_text(content)

        sf = SmartFixer(tmp_path)
        sf.fix_all(["test.py"])
        fixed = path.read_text()

        # The raise line should be fixed
        assert 'raise ValueError("cleanup failed") from e' in fixed
        # Everything else should be exactly preserved
        assert "# This is a header comment" in fixed
        assert "import os" in fixed
        assert "class Bar:" in fixed
        assert '    os.remove("file.txt")' in fixed

    def test_no_b904_no_change(self, tmp_path: Path):
        """File without B904 issues should be returned unchanged."""
        content = "x = 1\ny = 2\n"
        path = tmp_path / "test.py"
        path.write_text(content)

        sf = SmartFixer(tmp_path)
        sf.fix_all(["test.py"])
        assert path.read_text() == content

    def test_already_chained_raise(self, tmp_path: Path):
        """A raise that already has 'from' should not be modified."""
        content = textwrap.dedent("""\
            def foo():
                try:
                    pass
                except Exception as exc:
                    raise RuntimeError("x") from exc
        """)
        path = tmp_path / "test.py"
        path.write_text(content)

        sf = SmartFixer(tmp_path)
        sf.fix_all(["test.py"])
        assert path.read_text() == content

    def test_nested_except_uses_correct_exception(self, tmp_path: Path):
        """Nested try/except must use the inner exception name, not the outer."""
        content = textwrap.dedent("""\
            def foo():
                try:
                    pass
                except Exception as outer:
                    try:
                        pass
                    except ValueError as inner:
                        raise TypeError("nested")
                    raise RuntimeError("outer")
        """)
        path = tmp_path / "test.py"
        path.write_text(content)

        sf = SmartFixer(tmp_path)
        sf.fix_all(["test.py"])
        fixed = path.read_text()

        # Inner raise should chain from 'inner', not 'outer'
        assert 'raise TypeError("nested") from inner' in fixed
        # Outer raise should chain from 'outer'
        assert 'raise RuntimeError("outer") from outer' in fixed
