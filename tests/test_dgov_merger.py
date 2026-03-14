import subprocess

from dgov.merger import _plumbing_merge


def test_plumbing_merge_preserves_dirty_working_tree(tmp_path):
    # Setup a git repo
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)

    # Initial commit
    (repo / "README.md").write_text("initial content\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True)

    # Create a branch with a change
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True)
    (repo / "feature.txt").write_text("feature content\n")
    subprocess.run(["git", "add", "feature.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "feature change"], cwd=repo, check=True)

    # Back to main
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True)

    # Create a dirty change in a tracked file
    (repo / "README.md").write_text("dirty content\n")

    # Perform plumbing merge
    result = _plumbing_merge(str(repo), "feature")
    assert result.success is True

    # Verify the merge commit includes the feature change
    feature_file = repo / "feature.txt"
    assert feature_file.exists()
    assert feature_file.read_text() == "feature content\n"

    # Verify the dirty content is PRESERVED (this failed before the fix)
    assert (repo / "README.md").read_text() == "dirty content\n"

    # Verify it is still dirty in status
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    )
    assert "M README.md" in status.stdout
