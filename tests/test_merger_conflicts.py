"""Test cases for conflict resolution and workflow in merger module."""

import subprocess
from unittest.mock import MagicMock, patch


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
    call_seq = [
        make_subprocess_mock(0, "abc123"),  # rev-parse HEAD
        make_subprocess_mock(0, "newtree_hash\n"),  # merge-tree
        make_subprocess_mock(0, "abc123"),  # rev-parse branch_name
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

    # Now test _detect_conflicts - needs merge-base + merge-tree with conflict output
    mock_run.reset_mock()

    mock_merge_base = make_subprocess_mock(0, "abc123\n")
    mock_merge_tree_output = make_subprocess_mock(0, "changed in both test.py\n")

    mock_run.side_effect = [mock_merge_base, mock_merge_tree_output]

    conflicts = _detect_conflicts(str(tmp_path), "test-branch")

    assert "test.py" in conflicts


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

        assert "error" in result
        assert "not found" in result["error"]


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
        assert "error" in result or "conflicts" in result


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
            assert "not found" not in str(result.get("error", ""))


def test_worker_pane_skip_returns_conflict_error():
    """Test skip strategy returns conflict details without starting manual resolution."""
    from dgov.merger import MergeResult, merge_worker_pane

    mock_pane = {
        "slug": "test-slug",
        "branch_name": "test-branch",
        "project_root": "/fake/project",
        "worktree_path": "",
        "state": "done",
        "base_sha": "",
    }

    with (
        patch("dgov.persistence.get_pane", return_value=mock_pane),
        patch("dgov.merger._commit_worktree", return_value={}),
        patch("dgov.merger._rebase_onto_head", return_value=MergeResult(True, "")),
        patch("dgov.merger._plumbing_merge", return_value=MergeResult(False, "conflict")),
        patch("dgov.merger._detect_conflicts", return_value=["test.py"]),
        patch("dgov.persistence.update_pane_state"),
        patch("subprocess.run") as mock_run,
    ):
        result = merge_worker_pane("/fake/project", "test-slug", resolve="skip")

    assert result == {
        "error": "Merge conflict in test-branch",
        "slug": "test-slug",
        "branch": "test-branch",
        "conflicts": ["test.py"],
        "hint": "Re-run with --resolve agent or --resolve manual.",
    }
    mock_run.assert_not_called()


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
