"""Tests for dgov.review_fix — parse findings, dedup, filter, pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dgov.review_fix import (
    ReviewFinding,
    ReviewParseError,
    _deduplicate,
    _filter_by_severity,
    _group_by_file,
    parse_review_findings,
    run_review_fix_pipeline,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# parse_review_findings
# ---------------------------------------------------------------------------


class TestParseReviewFindings:
    def test_valid_json_array(self) -> None:
        output = json.dumps(
            [
                {
                    "file": "src/foo.py",
                    "line": 42,
                    "severity": "medium",
                    "category": "bug",
                    "description": "Off-by-one",
                    "suggested_fix": "Change < to <=",
                }
            ]
        )
        findings = parse_review_findings(output)
        assert len(findings) == 1
        assert findings[0].file == "src/foo.py"
        assert findings[0].line == 42
        assert findings[0].severity == "medium"
        assert findings[0].category == "bug"

    def test_multiple_findings(self) -> None:
        output = json.dumps(
            [
                {
                    "file": "a.py",
                    "line": 1,
                    "severity": "critical",
                    "category": "security",
                    "description": "SQL injection",
                    "suggested_fix": "Use parameterized queries",
                },
                {
                    "file": "b.py",
                    "line": 10,
                    "severity": "low",
                    "category": "style",
                    "description": "Unused import",
                    "suggested_fix": "Remove it",
                },
            ]
        )
        findings = parse_review_findings(output)
        assert len(findings) == 2
        assert findings[0].severity == "critical"
        assert findings[1].severity == "low"

    def test_empty_array(self) -> None:
        assert parse_review_findings("[]") == []

    def test_empty_string(self) -> None:
        assert parse_review_findings("") == []

    def test_none_input(self) -> None:
        # Technically str, but test edge
        assert parse_review_findings("") == []

    def test_malformed_json_raises(self) -> None:
        with pytest.raises(ReviewParseError, match="No JSON array found"):
            parse_review_findings("not json at all{{{")

    def test_invalid_json_array_raises(self) -> None:
        with pytest.raises(ReviewParseError, match="Failed to parse JSON"):
            parse_review_findings("[invalid json content]")

    def test_json_with_markdown_fences(self) -> None:
        finding = {
            "file": "x.py",
            "line": 1,
            "severity": "low",
            "category": "style",
            "description": "test",
            "suggested_fix": "",
        }
        output = f"```json\n{json.dumps([finding])}\n```"
        findings = parse_review_findings(output)
        assert len(findings) == 1
        assert findings[0].file == "x.py"

    def test_json_embedded_in_text(self) -> None:
        finding = {
            "file": "y.py",
            "line": 5,
            "severity": "medium",
            "category": "bug",
            "description": "bad logic",
            "suggested_fix": "fix it",
        }
        output = f"Here are my findings:\n{json.dumps([finding])}\nDone!"
        findings = parse_review_findings(output)
        assert len(findings) == 1
        assert findings[0].file == "y.py"

    def test_partial_fields_uses_defaults(self) -> None:
        output = json.dumps([{"file": "z.py", "description": "something"}])
        findings = parse_review_findings(output)
        assert len(findings) == 1
        assert findings[0].line == 0
        assert findings[0].severity == "low"
        assert findings[0].category == ""

    def test_non_array_json_raises(self) -> None:
        # A dict without brackets hits "No JSON array found"
        output = json.dumps({"not": "an array"})
        with pytest.raises(ReviewParseError, match="No JSON array found"):
            parse_review_findings(output)

    def test_array_with_non_dict_items(self) -> None:
        output = json.dumps(["string", 42, None])
        assert parse_review_findings(output) == []


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDeduplicate:
    def test_removes_exact_duplicates(self) -> None:
        f1 = ReviewFinding("a.py", 10, "medium", "bug", "desc1", "fix1")
        f2 = ReviewFinding("a.py", 10, "medium", "bug", "desc2", "fix2")
        result = _deduplicate([f1, f2])
        assert len(result) == 1
        assert result[0] is f1  # first one wins

    def test_keeps_different_lines(self) -> None:
        f1 = ReviewFinding("a.py", 10, "medium", "bug", "desc", "")
        f2 = ReviewFinding("a.py", 20, "medium", "bug", "desc", "")
        result = _deduplicate([f1, f2])
        assert len(result) == 2

    def test_keeps_different_categories(self) -> None:
        f1 = ReviewFinding("a.py", 10, "medium", "bug", "desc", "")
        f2 = ReviewFinding("a.py", 10, "medium", "security", "desc", "")
        result = _deduplicate([f1, f2])
        assert len(result) == 2

    def test_keeps_different_files(self) -> None:
        f1 = ReviewFinding("a.py", 10, "medium", "bug", "desc", "")
        f2 = ReviewFinding("b.py", 10, "medium", "bug", "desc", "")
        result = _deduplicate([f1, f2])
        assert len(result) == 2

    def test_empty_input(self) -> None:
        assert _deduplicate([]) == []


# ---------------------------------------------------------------------------
# Severity filtering
# ---------------------------------------------------------------------------


class TestSeverityFilter:
    def _make_findings(self) -> list[ReviewFinding]:
        return [
            ReviewFinding("a.py", 1, "critical", "security", "critical issue", ""),
            ReviewFinding("b.py", 2, "medium", "bug", "medium issue", ""),
            ReviewFinding("c.py", 3, "low", "style", "low issue", ""),
        ]

    def test_critical_only(self) -> None:
        result = _filter_by_severity(self._make_findings(), "critical")
        assert len(result) == 1
        assert result[0].severity == "critical"

    def test_medium_includes_critical(self) -> None:
        result = _filter_by_severity(self._make_findings(), "medium")
        assert len(result) == 2
        assert {f.severity for f in result} == {"critical", "medium"}

    def test_low_includes_all(self) -> None:
        result = _filter_by_severity(self._make_findings(), "low")
        assert len(result) == 3

    def test_empty_input(self) -> None:
        assert _filter_by_severity([], "medium") == []


# ---------------------------------------------------------------------------
# Group by file
# ---------------------------------------------------------------------------


class TestGroupByFile:
    def test_groups_correctly(self) -> None:
        findings = [
            ReviewFinding("a.py", 1, "medium", "bug", "d1", ""),
            ReviewFinding("b.py", 2, "medium", "bug", "d2", ""),
            ReviewFinding("a.py", 10, "low", "style", "d3", ""),
        ]
        groups = _group_by_file(findings)
        assert len(groups) == 2
        assert len(groups["a.py"]) == 2
        assert len(groups["b.py"]) == 1


# ---------------------------------------------------------------------------
# run_review_fix_pipeline — review phase only (auto_approve=False)
# ---------------------------------------------------------------------------


class TestPipelineReviewOnly:
    @patch("dgov.lifecycle.close_worker_pane")
    @patch("dgov.status.capture_worker_output")
    @patch("dgov.executor.run_wait_slugs")
    @patch("dgov.persistence.get_pane")
    @patch("dgov.executor.run_dispatch_only")
    @patch("dgov.review_fix.emit_event")
    def test_review_only_returns_findings(
        self,
        mock_emit,
        mock_create,
        mock_get_pane,
        mock_is_done,
        mock_capture,
        mock_close,
        tmp_path: Path,
    ):
        mock_create.return_value = MagicMock(slug="review-000-foo")
        mock_is_done.return_value = True
        mock_get_pane.return_value = {"slug": "review-000-foo", "pane_id": "%1"}
        mock_capture.return_value = json.dumps(
            [
                {
                    "file": "src/foo.py",
                    "line": 42,
                    "severity": "medium",
                    "category": "bug",
                    "description": "Off-by-one",
                    "suggested_fix": "fix",
                }
            ]
        )

        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "foo.py").touch()

        result = run_review_fix_pipeline(
            project_root=str(tmp_path),
            targets=["src/foo.py"],
            session_root=str(tmp_path),
            auto_approve=False,
        )

        assert result["phase"] == "review_only"
        assert result["findings_count"] == 1
        assert len(result["findings"]) == 1
        assert result["findings"][0]["file"] == "src/foo.py"

    @patch("dgov.lifecycle.close_worker_pane")
    @patch("dgov.status.capture_worker_output")
    @patch("dgov.executor.run_wait_slugs")
    @patch("dgov.persistence.get_pane")
    @patch("dgov.executor.run_dispatch_only")
    @patch("dgov.review_fix.emit_event")
    def test_review_only_no_findings(
        self,
        mock_emit,
        mock_create,
        mock_get_pane,
        mock_is_done,
        mock_capture,
        mock_close,
        tmp_path: Path,
    ):
        mock_create.return_value = MagicMock(slug="review-000-bar")
        mock_is_done.return_value = True
        mock_get_pane.return_value = {"slug": "review-000-bar", "pane_id": "%1"}
        mock_capture.return_value = "[]"

        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "bar.py").touch()

        result = run_review_fix_pipeline(
            project_root=str(tmp_path),
            targets=["src/bar.py"],
            session_root=str(tmp_path),
            auto_approve=False,
        )

        assert result["phase"] == "review_only"
        assert result["findings_count"] == 0
        assert result["findings"] == []


# ---------------------------------------------------------------------------
# run_review_fix_pipeline — parse error handling
# ---------------------------------------------------------------------------


class TestPipelineParseError:
    @patch("dgov.lifecycle.close_worker_pane")
    @patch("dgov.status.capture_worker_output")
    @patch("dgov.executor.run_wait_slugs")
    @patch("dgov.persistence.get_pane")
    @patch("dgov.executor.run_dispatch_only")
    @patch("dgov.review_fix.emit_event")
    def test_malformed_output_skipped_not_treated_as_clean(
        self,
        mock_emit,
        mock_create,
        mock_get_pane,
        mock_is_done,
        mock_capture,
        mock_close,
        tmp_path: Path,
    ):
        mock_create.return_value = MagicMock(slug="review-000-bad")
        mock_is_done.return_value = True
        mock_get_pane.return_value = {"slug": "review-000-bad", "pane_id": "%1"}
        mock_capture.return_value = "I couldn't complete the review, sorry!"

        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "bad.py").touch()

        result = run_review_fix_pipeline(
            project_root=str(tmp_path),
            targets=["src/bad.py"],
            session_root=str(tmp_path),
            auto_approve=False,
        )

        assert result["phase"] == "review_only"
        assert result["findings_count"] == 0
        assert result["findings"] == []
        # Verify parse error event was emitted
        parse_error_calls = [
            c for c in mock_emit.call_args_list if c[0][1] == "review_fix_parse_error"
        ]
        assert len(parse_error_calls) == 1


# ---------------------------------------------------------------------------
# run_review_fix_pipeline — full pipeline (auto_approve=True)
# ---------------------------------------------------------------------------


class TestPipelineFull:
    @patch("dgov.lifecycle.close_worker_pane")
    @patch("dgov.executor.run_review_merge")
    @patch("dgov.status.capture_worker_output")
    @patch("dgov.executor.run_wait_slugs")
    @patch("dgov.persistence.get_pane")
    @patch("dgov.executor.run_dispatch_only")
    @patch("dgov.review_fix.emit_event")
    def test_full_pipeline_merges(
        self,
        mock_emit,
        mock_create,
        mock_get_pane,
        mock_is_done,
        mock_capture,
        mock_merge,
        mock_close,
        tmp_path: Path,
    ):
        # create_worker_pane returns different slugs for review vs fix
        call_count = {"n": 0}

        def create_side_effect(**kwargs):
            call_count["n"] += 1
            slug = kwargs.get("slug", f"worker-{call_count['n']}")
            return MagicMock(slug=slug)

        mock_create.side_effect = create_side_effect
        mock_is_done.return_value = True
        mock_get_pane.return_value = {"slug": "test", "pane_id": "%1"}
        mock_capture.return_value = json.dumps(
            [
                {
                    "file": "src/foo.py",
                    "line": 42,
                    "severity": "critical",
                    "category": "bug",
                    "description": "SQL injection",
                    "suggested_fix": "Use params",
                }
            ]
        )
        mock_merge.return_value = MagicMock(
            error=None,
            merge_result={"merged": "fix-foo", "branch": "fix-foo", "tests_passed": True},
        )

        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "foo.py").touch()

        result = run_review_fix_pipeline(
            project_root=str(tmp_path),
            targets=["src/foo.py"],
            session_root=str(tmp_path),
            auto_approve=True,
            severity_threshold="medium",
        )

        assert result["phase"] == "complete"
        assert result["findings_count"] == 1
        assert result["merged_count"] == 1
        assert result["failed_count"] == 0
        assert result["test_status"] == "pass"
        assert mock_close.call_args_list == [
            ((str(tmp_path), "review-000-foo"), {"session_root": str(tmp_path), "force": True}),
        ]

    @patch("dgov.lifecycle.close_worker_pane")
    @patch("dgov.status.capture_worker_output")
    @patch("dgov.executor.run_wait_slugs")
    @patch("dgov.persistence.get_pane")
    @patch("dgov.executor.run_dispatch_only")
    @patch("dgov.review_fix.emit_event")
    def test_full_pipeline_no_findings_skips_fix(
        self,
        mock_emit,
        mock_create,
        mock_get_pane,
        mock_is_done,
        mock_capture,
        mock_close,
        tmp_path: Path,
    ):
        mock_create.return_value = MagicMock(slug="review-000-empty")
        mock_is_done.return_value = True
        mock_get_pane.return_value = {"slug": "review-000-empty", "pane_id": "%1"}
        mock_capture.return_value = "[]"

        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "clean.py").touch()

        result = run_review_fix_pipeline(
            project_root=str(tmp_path),
            targets=["src/clean.py"],
            session_root=str(tmp_path),
            auto_approve=True,
        )

        assert result["phase"] == "complete"
        assert result["findings_count"] == 0
        assert result["fixed_count"] == 0
        assert result["test_status"] == "skipped"

    @patch("dgov.lifecycle.close_worker_pane")
    @patch("dgov.executor.run_review_merge")
    @patch("dgov.status.capture_worker_output")
    @patch("dgov.executor.run_wait_slugs")
    @patch("dgov.persistence.get_pane")
    @patch("dgov.executor.run_dispatch_only")
    @patch("dgov.review_fix.emit_event")
    def test_full_pipeline_merge_failure(
        self,
        mock_emit,
        mock_create,
        mock_get_pane,
        mock_is_done,
        mock_capture,
        mock_merge,
        mock_close,
        tmp_path: Path,
    ):
        call_count = {"n": 0}

        def create_side_effect(**kwargs):
            call_count["n"] += 1
            slug = kwargs.get("slug", f"worker-{call_count['n']}")
            return MagicMock(slug=slug)

        mock_create.side_effect = create_side_effect
        mock_is_done.return_value = True
        mock_get_pane.return_value = {"slug": "test", "pane_id": "%1"}
        mock_capture.return_value = json.dumps(
            [
                {
                    "file": "src/foo.py",
                    "line": 1,
                    "severity": "critical",
                    "category": "bug",
                    "description": "crash",
                    "suggested_fix": "fix",
                }
            ]
        )
        mock_merge.return_value = MagicMock(
            error="Merge failed",
            merge_result={"error": "Merge failed"},
        )

        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "foo.py").touch()

        result = run_review_fix_pipeline(
            project_root=str(tmp_path),
            targets=["src/foo.py"],
            session_root=str(tmp_path),
            auto_approve=True,
        )

        assert result["phase"] == "complete"
        assert result["failed_count"] == 1
        assert result["merged_count"] == 0
        mock_close.assert_called_once_with(
            str(tmp_path), "review-000-foo", session_root=str(tmp_path), force=True
        )

    @patch("dgov.lifecycle.close_worker_pane")
    @patch("dgov.executor.run_review_merge")
    @patch("dgov.status.capture_worker_output")
    @patch("dgov.executor.run_wait_slugs")
    @patch("dgov.persistence.get_pane")
    @patch("dgov.executor.run_dispatch_only")
    @patch("dgov.review_fix.emit_event")
    def test_full_pipeline_keeps_failed_fix_worker_open(
        self,
        mock_emit,
        mock_create,
        mock_get_pane,
        mock_is_done,
        mock_capture,
        mock_merge,
        mock_close,
        tmp_path: Path,
    ):
        call_count = {"n": 0}

        def create_side_effect(**kwargs):
            call_count["n"] += 1
            slug = kwargs.get("slug", f"worker-{call_count['n']}")
            return MagicMock(slug=slug)

        mock_create.side_effect = create_side_effect
        mock_is_done.return_value = True
        mock_get_pane.return_value = {"slug": "test", "pane_id": "%1"}
        mock_capture.return_value = json.dumps(
            [
                {
                    "file": "src/foo.py",
                    "line": 1,
                    "severity": "critical",
                    "category": "bug",
                    "description": "crash",
                    "suggested_fix": "fix",
                }
            ]
        )
        mock_merge.return_value = MagicMock(
            error="Merge failed",
            merge_result={"error": "Merge failed"},
        )

        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "foo.py").touch()

        result = run_review_fix_pipeline(
            project_root=str(tmp_path),
            targets=["src/foo.py"],
            session_root=str(tmp_path),
            auto_approve=True,
        )

        assert result["phase"] == "complete"
        assert result["failed_count"] == 1
        assert result["merged_count"] == 0
        # Review worker closes after capture; failed fix worker stays open for recovery.
        mock_close.assert_called_once_with(
            str(tmp_path), "review-000-foo", session_root=str(tmp_path), force=True
        )

    @patch("dgov.lifecycle.close_worker_pane")
    @patch("dgov.executor.run_review_merge")
    @patch("dgov.status.capture_worker_output")
    @patch("dgov.executor.run_wait_slugs")
    @patch("dgov.persistence.get_pane")
    @patch("dgov.executor.run_dispatch_only")
    @patch("dgov.review_fix.emit_event")
    def test_full_pipeline_surfaces_merge_validation_failure(
        self,
        mock_emit,
        mock_create,
        mock_get_pane,
        mock_is_done,
        mock_capture,
        mock_merge,
        mock_close,
        tmp_path: Path,
    ):
        mock_create.side_effect = lambda **kwargs: MagicMock(slug=kwargs.get("slug", "worker"))
        mock_is_done.return_value = True
        mock_get_pane.return_value = {"slug": "test", "pane_id": "%1"}
        mock_capture.return_value = json.dumps(
            [
                {
                    "file": "src/foo.py",
                    "line": 1,
                    "severity": "critical",
                    "category": "bug",
                    "description": "crash",
                    "suggested_fix": "fix",
                }
            ]
        )
        mock_merge.return_value = MagicMock(
            error=None,
            merge_result={"merged": "fix-foo", "branch": "fix-foo", "tests_passed": False},
        )

        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "foo.py").touch()

        result = run_review_fix_pipeline(
            project_root=str(tmp_path),
            targets=["src/foo.py"],
            session_root=str(tmp_path),
            auto_approve=True,
        )

        assert result["phase"] == "complete"
        assert result["merged_count"] == 1
        assert result["test_status"] == "failures:fix-foo"


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------


class TestReviewFixCLI:
    def test_cli_help(self) -> None:
        from click.testing import CliRunner

        from dgov.cli import cli

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["review-fix", "--help"],
            env={"DGOV_SKIP_GOVERNOR_CHECK": "1"},
        )
        assert result.exit_code == 0
        assert "--targets" in result.output
        assert "--auto-approve" in result.output
        assert "--severity" in result.output

    @patch("dgov.review_fix.run_review_fix_pipeline")
    def test_cli_review_only(self, mock_pipeline) -> None:
        from click.testing import CliRunner

        from dgov.cli import cli

        mock_pipeline.return_value = {
            "phase": "review_only",
            "findings_count": 2,
            "findings": [
                {
                    "file": "a.py",
                    "line": 1,
                    "severity": "medium",
                    "category": "bug",
                    "description": "issue",
                    "suggested_fix": "",
                }
            ],
            "all_findings_count": 2,
            "filtered_out": 0,
        }

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["review-fix", "-t", "src/", "--project-root", "/tmp/fake"],
            env={"DGOV_SKIP_GOVERNOR_CHECK": "1"},
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["phase"] == "review_only"
        assert data["findings_count"] == 2
