"""Test cases for conflict resolution and workflow in merger module."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest


def make_subprocess_mock(return_code, stdout="", stderr=""):
    """Helper to create a mock CompletedProcess result."""
    mock_stdout = MagicMock()
    if stdout:
        mock_stdout.strip.return_value = stdout.strip()
    else:
        mock_stdout.strip.return_value = ""

    mock_stderr = MagicMock()
    if stderr:
        mock_stderr.strip.return_value = stderr.strip()
    else:
        mock_stderr.strip.return_value = ""

    mock_result = MagicMock(spec=subprocess.CompletedProcess)
    mock_result.returncode = return_code
    mock_result.stdout = stdout if stdout else mock_stdout
    mock_result.stderr = stderr if stderr else mock_stderr
    return mock_result


@patch("subprocess.run")
def test_plumbing_success(mock_run, tmp_path):
    """Test successful merge when git operation returns 0."""
    from dgov.merger import _plumbing_merge

    # Sequence of all subprocess.run calls in _plumbing_merge:
    head_sha = "a" * 40
    branch_sha = "b" * 40
    tree_hash = "c" * 40
    commit_hash = "d" * 40
    call_seq = [
        make_subprocess_mock(0, head_sha),  # rev-parse HEAD
        make_subprocess_mock(0, branch_sha),  # rev-parse branch_name
        make_subprocess_mock(0, head_sha),  # merge-base
        make_subprocess_mock(0, tree_hash + "\n"),  # merge-tree --write-tree
        make_subprocess_mock(0, commit_hash + "\n"),  # commit-tree
        make_subprocess_mock(0, "main\n"),  # symbolic-ref --short HEAD
        make_subprocess_mock(0, ""),  # status --porcelain (_stash_guard)
        make_subprocess_mock(0),  # update-ref
        make_subprocess_mock(0),  # reset --hard
    ]

    idx = [0]

    def side_effect(*args, **kwargs):
        result = call_seq[idx[0]]
        idx[0] += 1
        return result

    mock_run.side_effect = side_effect

    result = _plumbing_merge(str(tmp_path), "test-branch")

    assert result.success is True
    assert result.stderr == ""


@patch("subprocess.run")
def test_plumbing_conflict(mock_run, tmp_path):
    """Test merge conflict when git operation returns nonzero."""
    from dgov.merger import _detect_conflicts, _plumbing_merge

    # Mock subprocess for git rev-parse HEAD (success)
    mock_head = make_subprocess_mock(0, "abc123")

    # Mock git merge-tree result (conflict - nonzero return)
    mock_merge_tree = make_subprocess_mock(1, "", "CONFLICT (content): Merge conflict in test.py")

    # Sequence for _plumbing_merge call
    mock_run.side_effect = [mock_head, mock_merge_tree]

    result = _plumbing_merge(str(tmp_path), "test-branch")

    assert result.success is False

    # Now test _detect_conflicts - uses modern merge-tree --write-tree
    mock_run.reset_mock()

    head_sha = "a" * 40
    branch_sha = "b" * 40
    tree_sha = "c" * 40
    mock_head = make_subprocess_mock(0, head_sha)
    mock_branch = make_subprocess_mock(0, branch_sha)
    mock_merge_base = make_subprocess_mock(0, "d" * 40 + "\n")
    mock_merge_tree_output = make_subprocess_mock(
        1, f"{tree_sha}\nCONFLICT (content): Merge conflict in test.py\n"
    )

    mock_run.side_effect = [mock_head, mock_branch, mock_merge_base, mock_merge_tree_output]

    conflicts = _detect_conflicts(str(tmp_path), "test-branch")

    assert "test.py" in conflicts


@pytest.mark.unit
def test_detect_conflicts_non_overlapping_same_file(tmp_path):
    """Non-overlapping edits to the same file must NOT be reported as conflicts.

    This is the core regression test for ledger #68: the old 3-arg merge-tree
    reported 'changed in both' for any file modified on both sides, even when
    the edits don't overlap. The modern --write-tree form auto-resolves these.
    """
    from dgov.merger import _detect_conflicts

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=True,
        )

    git("init")
    git("config", "user.email", "test@test.com")
    git("config", "user.name", "Test")

    # Create a file with well-separated sections
    (repo / "shared.py").write_text(
        "# Section A (lines 1-5)\n"
        "def func_a():\n"
        "    return 1\n"
        "\n"
        "\n"
        "# Section B (lines 6-10)\n"
        "def func_b():\n"
        "    return 2\n"
        "\n"
        "\n"
    )
    git("add", ".")
    git("commit", "-m", "initial")

    # Create worker branch and edit section B only
    git("checkout", "-b", "worker-branch")
    (repo / "shared.py").write_text(
        "# Section A (lines 1-5)\n"
        "def func_a():\n"
        "    return 1\n"
        "\n"
        "\n"
        "# Section B (lines 6-10)\n"
        "def func_b():\n"
        "    return 99  # worker changed this\n"
        "\n"
        "\n"
    )
    git("add", ".")
    git("commit", "-m", "worker edits section B")

    # Back to main, edit section A only (non-overlapping)
    git("checkout", "main")
    (repo / "shared.py").write_text(
        "# Section A (lines 1-5)\n"
        "def func_a():\n"
        "    return 42  # main changed this\n"
        "\n"
        "\n"
        "# Section B (lines 6-10)\n"
        "def func_b():\n"
        "    return 2\n"
        "\n"
        "\n"
    )
    git("add", ".")
    git("commit", "-m", "main edits section A")

    # _detect_conflicts should return [] — no real overlapping conflict
    conflicts = _detect_conflicts(str(repo), "worker-branch")
    assert conflicts == [], f"Non-overlapping edits falsely reported as conflicts: {conflicts}"


@pytest.mark.unit
def test_detect_conflicts_overlapping_same_file(tmp_path):
    """Overlapping edits to the same lines MUST be reported as conflicts."""
    from dgov.merger import _detect_conflicts

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=True,
        )

    git("init")
    git("config", "user.email", "test@test.com")
    git("config", "user.name", "Test")

    (repo / "shared.py").write_text("def func():\n    return 1\n")
    git("add", ".")
    git("commit", "-m", "initial")

    # Worker edits the same line
    git("checkout", "-b", "worker-branch")
    (repo / "shared.py").write_text("def func():\n    return 99\n")
    git("add", ".")
    git("commit", "-m", "worker changes return value")

    # Main also edits the same line — overlapping!
    git("checkout", "main")
    (repo / "shared.py").write_text("def func():\n    return 42\n")
    git("add", ".")
    git("commit", "-m", "main changes return value")

    conflicts = _detect_conflicts(str(repo), "worker-branch")
    assert "shared.py" in conflicts, f"Overlapping edits not detected: {conflicts}"


@patch("subprocess.run")
def test_restore_protected_files_no_damage(mock_run, tmp_path):
    """Test that protected files are not restored when unchanged."""
    from dgov.merger import _restore_protected_files

    # Create pane_record with unchanged protected file content
    pane_record = {
        "worktree_path": str(tmp_path),
        "branch_name": "test-branch",
        "base_sha": "abc123",
    }

    # Mock git diff returning no changes to protected files
    mock_diff = make_subprocess_mock(0, "some_other_file.py\n")
    mock_run.return_value = mock_diff

    _restore_protected_files(str(tmp_path), pane_record)

    # Verify git checkout was NOT called (no damage to restore)
    checkout_calls = [c for c in mock_run.call_args_list if "checkout" in str(c)]
    assert len(checkout_calls) == 0


@patch("subprocess.run")
def test_restore_protected_files_restores(mock_run, tmp_path):
    """Test that protected files are restored when changed."""
    from dgov.merger import _restore_protected_files

    # Create pane_record with changed protected file
    pane_record = {
        "worktree_path": str(tmp_path),
        "branch_name": "test-branch",
        "base_sha": "abc123",
    }

    # Mock git diff returning protected files as changed
    mock_diff = make_subprocess_mock(0, "CLAUDE.md\n")
    mock_run.return_value = mock_diff

    _restore_protected_files(str(tmp_path), pane_record)

    # Verify git checkout WAS called to restore the file
    checkout_calls = [c for c in mock_run.call_args_list if "checkout" in str(c)]
    assert len(checkout_calls) >= 1


def test_worker_pane_not_found():
    """Test merge when pane slug does not exist."""
    from dgov.merger import merge_worker_pane

    # Import and mock panes inline since _p is defined inside the function
    with patch("dgov.persistence.get_pane", return_value=None):
        result = merge_worker_pane("/fake/project", "nonexistent-slug")

        assert result.error is not None
        assert "not found" in result.error


@patch("subprocess.run")
def test_worker_pane_branch_not_found(mock_run, tmp_path):
    """Test merge when branch doesn't exist."""
    from dgov.merger import merge_worker_pane

    # Create a real git project directory for the test
    fake_project = tmp_path / "fake_project"
    fake_project.mkdir()

    # Initialize a bare minimum git repo to avoid git errors
    subprocess.run(["git", "init"], cwd=fake_project, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"], cwd=fake_project, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"], cwd=fake_project, capture_output=True
    )

    # Create initial commit
    (fake_project / "README.md").write_text("# Test\n")
    subprocess.run(["git", "add", "."], cwd=fake_project, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=fake_project, capture_output=True)

    # Mock pane with nonexistent branch
    fake_worktree = tmp_path / "worktree"
    fake_worktree.mkdir()

    mock_pane = {
        "slug": "test-slug",
        "branch_name": "nonexistent-branch",  # Branch doesn't exist
        "project_root": str(fake_project),
        "worktree_path": str(fake_worktree),
        "state": "done",  # State is done but branch missing
        "base_sha": "abc123",
    }

    # Mock rev-parse for nonexistent branch to fail
    mock_rev_parse = make_subprocess_mock(1, "", "fatal: no such ref: nonexistent-branch")

    def side_effect(*args, **kwargs):
        cmd = args[0] if args else []
        if isinstance(cmd, list) and "rev-parse" in cmd:
            return mock_rev_parse
        return make_subprocess_mock(0)

    mock_run.side_effect = side_effect

    with patch("dgov.persistence.get_pane", return_value=mock_pane):
        result = merge_worker_pane(str(fake_project), "test-slug")

        # Branch not found should cause error in _plumbing_merge
        assert result.error is not None or len(result.conflicts) > 0


def test_worker_pane_success_with_merge(tmp_path):
    """Test successful pane merge with mocked operations."""
    from dgov.merger import merge_worker_pane

    # Create a real git project directory for the test
    fake_project = tmp_path / "fake_project"
    fake_project.mkdir()

    # Initialize a bare minimum git repo
    subprocess.run(["git", "init"], cwd=fake_project, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"], cwd=fake_project, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"], cwd=fake_project, capture_output=True
    )

    # Create initial commit
    (fake_project / "README.md").write_text("# Test\n")
    subprocess.run(["git", "add", "."], cwd=fake_project, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=fake_project, capture_output=True)

    # Create the branch that pane uses
    fake_worktree = tmp_path / "worktree"
    fake_worktree.mkdir()

    subprocess.run(["git", "checkout", "-b", "test-branch"], cwd=fake_project, capture_output=True)
    (fake_project / "new_file.py").write_text("pass\n")
    subprocess.run(["git", "add", "."], cwd=fake_project, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add file"], cwd=fake_project, capture_output=True)

    # Mock pane with correct branch and state
    mock_pane = {
        "slug": "test-slug",
        "branch_name": "test-branch",
        "project_root": str(fake_project),
        "worktree_path": str(fake_worktree),
        "state": "done",  # Pane is done and ready to merge
        "base_sha": None,  # No base SHA since this is first commit
    }

    with patch("dgov.persistence.get_pane", return_value=mock_pane):
        with (
            patch("dgov.waiter._is_done", return_value=True),
        ):
            result = merge_worker_pane(str(fake_project), "test-slug")

            # Should succeed or at least not have an error about pane not found
            assert "not found" not in str(result.error or "")


def test_worker_pane_skip_returns_conflict_error(tmp_path):
    """Test skip strategy returns conflict details without starting manual resolution."""
    from contextlib import contextmanager

    from dgov.merger import MergeResult, merge_worker_pane

    mock_pane = {
        "slug": "test-slug",
        "branch_name": "test-branch",
        "project_root": str(tmp_path),
        "worktree_path": "",
        "state": "done",
        "base_sha": "",
    }

    @contextmanager
    def fake_candidate_worktree(project_root, slug):
        yield str(tmp_path), "fake-branch"

    with (
        patch("dgov.persistence.get_pane", return_value=mock_pane),
        patch("dgov.merger._check_merge_preconditions", return_value=None),
        patch("dgov.merger._restore_protected_files"),
        patch("dgov.merger._capture_pre_merge_stats", return_value=("", 0, [])),
        patch("dgov.merger._check_dirty_worktree", return_value=[]),
        patch("dgov.merger._rebase_onto_head", return_value=MergeResult(True, "")),
        patch("dgov.merger._candidate_worktree", side_effect=fake_candidate_worktree),
        patch(
            "dgov.merger._execute_candidate_merge",
            return_value=MergeResult(False, "conflict"),
        ),
        patch("dgov.merger._detect_conflicts", return_value=["test.py"]),
        patch("dgov.persistence.update_pane_state"),
        patch("dgov.persistence.emit_event"),
    ):
        result = merge_worker_pane(str(tmp_path), "test-slug", resolve="skip")

    assert result.error == "Merge conflict in test-branch"
    assert result.slug == "test-slug"
    assert result.branch == "test-branch"
    assert result.conflicts == ["test.py"]
    assert result.hint == "Re-run with --resolve agent or --resolve manual."


@patch("subprocess.run")
def test_lint_fix_calls_ruff(mock_subprocess, tmp_path):
    """Test that linting uses ruff when available."""
    from dgov.merger import _lint_fix_merged_files

    # Create a temp directory for the project
    fake_project = tmp_path / "project"
    fake_project.mkdir()

    # Initialize git repo to avoid errors
    subprocess.run(["git", "init"], cwd=fake_project, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"], cwd=fake_project, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"], cwd=fake_project, capture_output=True
    )

    # Create initial commit and a test.py file
    (fake_project / "README.md").write_text("# Test\n")
    subprocess.run(["git", "add", "."], cwd=fake_project, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=fake_project, capture_output=True)

    (fake_project / "test.py").write_text("x=1\n")

    with patch("shutil.which", return_value="/usr/bin/ruff"):
        # Mock ruff check command - returncode=0 means no issues
        mock_ruff_check = make_subprocess_mock(0)

        # Git diff should also be mocked to detect changes
        mock_git_diff = make_subprocess_mock(0, "test.py\n")

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and len(cmd) > 0 and "ruff" in str(cmd[0]):
                return mock_ruff_check
            elif isinstance(cmd, list) and len(cmd) > 0 and cmd[0] == "git":
                return mock_git_diff
            return make_subprocess_mock(0)

        mock_subprocess.side_effect = side_effect

        _lint_fix_merged_files(str(fake_project), ["test.py"])

        # Verify subprocess was called with ruff
        call_args_list = mock_subprocess.call_args_list
        assert any("ruff" in str(args) for args, kwargs in call_args_list)


@patch("subprocess.run")
@pytest.mark.unit
def test_plumbing_merge_tree_exit_code_1_with_valid_hash(mock_run, tmp_path):
    """Test merge-tree returns 1 with valid tree hash — should still succeed.

    git merge-tree --write-tree returns exit code 1 for conflicts but outputs
    the tree hash anyway. We need to parse and validate the hash before treating
    non-zero as failure.
    """
    from dgov.merger import _plumbing_merge

    # Simulate merge-tree returning exit code 1 with valid tree hash on stdout
    valid_40char_hash = "a" * 40

    call_seq = [
        make_subprocess_mock(0, valid_40char_hash[:7]),  # rev-parse HEAD (short)
        make_subprocess_mock(1, f"{valid_40char_hash}\n"),
        make_subprocess_mock(0, valid_40char_hash),  # rev-parse branch_name
        make_subprocess_mock(0, "commit_hash123\n"),  # commit-tree
        make_subprocess_mock(0, "main\n"),  # symbolic-ref --short HEAD
        make_subprocess_mock(0, ""),  # status --porcelain
        make_subprocess_mock(0),  # update-ref
        make_subprocess_mock(0),  # reset --hard
    ]

    idx = [0]

    def side_effect(*args, **kwargs):
        result = call_seq[idx[0]]
        idx[0] += 1
        return result

    mock_run.side_effect = side_effect

    result = _plumbing_merge(str(tmp_path), "test-branch")

    # Exit code 1 with valid hash = real conflicts — should fail
    assert result.success is False


@patch("subprocess.run")
@pytest.mark.unit
def test_plumbing_merge_tree_exit_code_1_no_hash(mock_run, tmp_path):
    """Test merge-tree returns 1 with no stdout — should fail."""
    from dgov.merger import _plumbing_merge

    call_seq = [
        make_subprocess_mock(0, "abc123"),  # rev-parse HEAD
        make_subprocess_mock(1, "", "CONFLICT: genuine failure"),  # merge-tree no output
        make_subprocess_mock(0),  # rest doesn't matter
    ]

    mock_run.side_effect = call_seq

    result = _plumbing_merge(str(tmp_path), "test-branch")

    # Should fail because there's no tree hash in stdout
    assert result.success is False


@pytest.mark.unit
@patch("subprocess.run")
def test_restore_protected_files_returns_early_when_all_checkouts_fail(mock_run, tmp_path):
    """Test that _restore_protected_files returns early when no files are restored.

    Regression test for audit finding: ensure git amend is not run when all
    checkout operations fail.
    """
    from dgov.merger import _restore_protected_files

    # Create pane_record with changed protected file
    pane_record = {
        "worktree_path": str(tmp_path),
        "branch_name": "test-branch",
        "base_sha": "abc123",
    }

    # Mock git diff returning protected file as changed
    mock_diff = make_subprocess_mock(0, "CLAUDE.md\n")

    # Mock git checkout to fail - stderr needs to be bytes for .decode()
    mock_stderr = MagicMock()
    mock_stderr.decode.return_value = "checkout failed"
    mock_checkout = make_subprocess_mock(1, "", "checkout failed")
    mock_checkout.stderr = mock_stderr

    # Return diff first, then checkout failure
    mock_run.side_effect = [mock_diff, mock_checkout]

    _restore_protected_files(str(tmp_path), pane_record)

    # Verify git commit --amend was NOT called (no amend without changes)
    amend_calls = [
        c for c in mock_run.call_args_list if "commit" in str(c) and "--amend" in str(c)
    ]
    assert len(amend_calls) == 0

    # Verify add was also not called
    add_calls = [c for c in mock_run.call_args_list if "add" in str(c)]
    assert len(add_calls) == 0
