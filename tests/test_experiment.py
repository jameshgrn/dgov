"""Tests for dgov.experiment — experiment log, single run, loop, CLI."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dgov.experiment import (
    ExperimentLog,
    _metric_improved,
    _read_result_file,
    run_experiment,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# ExperimentLog
# ---------------------------------------------------------------------------


class TestExperimentLog:
    def test_append_and_read(self, tmp_path: Path) -> None:
        log = ExperimentLog(str(tmp_path), "test-prog")
        entry = {
            "id": "exp-001",
            "hypothesis": "try deeper model",
            "metric_name": "val_bpb",
            "metric_before": 1.42,
            "metric_after": 1.38,
            "status": "accepted",
            "agent": "pi",
            "duration_s": 100,
            "follow_ups": ["try wider"],
            "commit_sha": "abc123",
        }
        log.append_result(entry)
        entries = log.read_log()
        assert len(entries) == 1
        assert entries[0]["id"] == "exp-001"
        assert entries[0]["status"] == "accepted"

    def test_read_empty(self, tmp_path: Path) -> None:
        log = ExperimentLog(str(tmp_path), "empty")
        assert log.read_log() == []

    def test_multiple_entries(self, tmp_path: Path) -> None:
        log = ExperimentLog(str(tmp_path), "multi")
        log.append_result({"id": "exp-1", "status": "accepted", "metric_after": 1.3})
        log.append_result({"id": "exp-2", "status": "rejected", "metric_after": 1.5})
        log.append_result({"id": "exp-3", "status": "accepted", "metric_after": 1.1})
        entries = log.read_log()
        assert len(entries) == 3

    def test_best_result_minimize(self, tmp_path: Path) -> None:
        log = ExperimentLog(str(tmp_path), "best-min")
        log.append_result({"id": "e1", "status": "accepted", "metric_after": 1.5})
        log.append_result({"id": "e2", "status": "rejected", "metric_after": 1.0})
        log.append_result({"id": "e3", "status": "accepted", "metric_after": 1.2})
        best = log.best_result("minimize")
        assert best is not None
        assert best["id"] == "e3"

    def test_best_result_maximize(self, tmp_path: Path) -> None:
        log = ExperimentLog(str(tmp_path), "best-max")
        log.append_result({"id": "e1", "status": "accepted", "metric_after": 0.8})
        log.append_result({"id": "e2", "status": "accepted", "metric_after": 0.95})
        best = log.best_result("maximize")
        assert best is not None
        assert best["id"] == "e2"

    def test_best_result_no_accepted(self, tmp_path: Path) -> None:
        log = ExperimentLog(str(tmp_path), "no-accepted")
        log.append_result({"id": "e1", "status": "rejected", "metric_after": 1.5})
        assert log.best_result() is None

    def test_summary(self, tmp_path: Path) -> None:
        log = ExperimentLog(str(tmp_path), "summ")
        log.append_result(
            {"id": "e1", "status": "accepted", "metric_after": 1.3, "duration_s": 60}
        )
        log.append_result(
            {"id": "e2", "status": "rejected", "metric_after": 1.5, "duration_s": 45}
        )
        log.append_result({"id": "e3", "status": "error", "metric_after": None, "duration_s": 10})
        s = log.summary()
        assert s["total"] == 3
        assert s["accepted"] == 1
        assert s["rejected"] == 1
        assert s["errored"] == 1
        assert s["total_duration_s"] == 115
        assert s["best"]["id"] == "e1"

    def test_summary_empty(self, tmp_path: Path) -> None:
        log = ExperimentLog(str(tmp_path), "empty-summ")
        s = log.summary()
        assert s["total"] == 0
        assert s["best"] is None

    def test_log_path(self, tmp_path: Path) -> None:
        log = ExperimentLog(str(tmp_path), "my-program")
        assert log.path == tmp_path / ".dgov" / "experiments" / "my-program.jsonl"


# ---------------------------------------------------------------------------
# Metric comparison
# ---------------------------------------------------------------------------


class TestMetricImproved:
    def test_minimize_improved(self) -> None:
        assert _metric_improved(1.5, 1.3, "minimize") is True

    def test_minimize_regressed(self) -> None:
        assert _metric_improved(1.3, 1.5, "minimize") is False

    def test_minimize_equal(self) -> None:
        assert _metric_improved(1.3, 1.3, "minimize") is False

    def test_maximize_improved(self) -> None:
        assert _metric_improved(0.8, 0.9, "maximize") is True

    def test_maximize_regressed(self) -> None:
        assert _metric_improved(0.9, 0.8, "maximize") is False

    def test_none_before(self) -> None:
        assert _metric_improved(None, 1.0, "minimize") is False

    def test_none_after(self) -> None:
        assert _metric_improved(1.0, None, "minimize") is False


# ---------------------------------------------------------------------------
# Result file parsing
# ---------------------------------------------------------------------------


class TestReadResultFile:
    def test_reads_valid_file(self, tmp_path: Path) -> None:
        results_dir = tmp_path / ".dgov" / "experiments" / "results"
        results_dir.mkdir(parents=True)
        result_file = results_dir / "exp-001.json"
        result_file.write_text(
            json.dumps(
                {
                    "metric_name": "val_bpb",
                    "metric_value": 1.38,
                    "hypothesis": "deeper model",
                    "follow_ups": ["try wider"],
                }
            )
        )
        result = _read_result_file(str(tmp_path), "exp-001")
        assert result is not None
        assert result["metric_value"] == 1.38

    def test_missing_file(self, tmp_path: Path) -> None:
        results_dir = tmp_path / ".dgov" / "experiments" / "results"
        results_dir.mkdir(parents=True)
        assert _read_result_file(str(tmp_path), "nonexistent") is None

    def test_invalid_json(self, tmp_path: Path) -> None:
        results_dir = tmp_path / ".dgov" / "experiments" / "results"
        results_dir.mkdir(parents=True)
        (results_dir / "bad.json").write_text("not json{{{")
        assert _read_result_file(str(tmp_path), "bad") is None


# ---------------------------------------------------------------------------
# run_experiment (mocked pane lifecycle)
# ---------------------------------------------------------------------------


class TestRunExperiment:
    @patch("dgov.experiment._read_result_file")
    @patch("dgov.panes.close_worker_pane")
    @patch("dgov.panes.merge_worker_pane")
    @patch("dgov.panes.wait_worker_pane")
    @patch("dgov.panes.create_worker_pane")
    @patch("dgov.experiment._emit_event")
    def test_accepted_on_improvement(
        self, mock_emit, mock_create, mock_wait, mock_merge, mock_close, mock_read_result, tmp_path
    ):
        mock_create.return_value = MagicMock(slug="exp-test01")
        mock_wait.return_value = {"done": "exp-test01", "method": "signal"}
        mock_merge.return_value = {"merged": "exp-test01", "branch": "exp-test01"}
        mock_read_result.return_value = {
            "metric_name": "val_bpb",
            "metric_value": 1.30,
            "hypothesis": "deeper net",
            "follow_ups": ["wider net"],
        }

        with patch("subprocess.run") as mock_subproc:
            mock_subproc.return_value = MagicMock(returncode=0, stdout="abc123\n")
            result = run_experiment(
                project_root=str(tmp_path),
                program_text="Try deeper net",
                metric_name="val_bpb",
                metric_baseline=1.42,
                agent="pi",
                session_root=str(tmp_path),
                exp_id="exp-test01",
            )

        assert result["status"] == "accepted"
        assert result["metric_after"] == 1.30
        assert result["follow_ups"] == ["wider net"]
        mock_merge.assert_called_once()

    @patch("dgov.experiment._read_result_file")
    @patch("dgov.panes.close_worker_pane")
    @patch("dgov.panes.merge_worker_pane")
    @patch("dgov.panes.wait_worker_pane")
    @patch("dgov.panes.create_worker_pane")
    @patch("dgov.experiment._emit_event")
    def test_rejected_on_regression(
        self, mock_emit, mock_create, mock_wait, mock_merge, mock_close, mock_read_result, tmp_path
    ):
        mock_create.return_value = MagicMock(slug="exp-test02")
        mock_wait.return_value = {"done": "exp-test02"}
        mock_read_result.return_value = {
            "metric_name": "val_bpb",
            "metric_value": 1.50,
            "hypothesis": "bad idea",
            "follow_ups": [],
        }

        result = run_experiment(
            project_root=str(tmp_path),
            program_text="Bad idea",
            metric_name="val_bpb",
            metric_baseline=1.42,
            agent="pi",
            session_root=str(tmp_path),
            exp_id="exp-test02",
        )

        assert result["status"] == "rejected"
        assert result["metric_after"] == 1.50
        assert result["commit_sha"] is None
        mock_merge.assert_not_called()
        mock_close.assert_called_once()

    @patch("dgov.experiment._read_result_file")
    @patch("dgov.panes.close_worker_pane")
    @patch("dgov.panes.wait_worker_pane")
    @patch("dgov.panes.create_worker_pane")
    @patch("dgov.experiment._emit_event")
    def test_error_on_missing_result_file(
        self, mock_emit, mock_create, mock_wait, mock_close, mock_read_result, tmp_path
    ):
        mock_create.return_value = MagicMock(slug="exp-test03")
        mock_wait.return_value = {"done": "exp-test03"}
        mock_read_result.return_value = None

        result = run_experiment(
            project_root=str(tmp_path),
            program_text="Something",
            metric_name="val_bpb",
            metric_baseline=1.42,
            agent="pi",
            session_root=str(tmp_path),
            exp_id="exp-test03",
        )

        assert result["status"] == "error"
        assert result["error"] == "no_result_file"
        mock_close.assert_called_once()

    @patch("dgov.panes.close_worker_pane")
    @patch("dgov.panes.wait_worker_pane")
    @patch("dgov.panes.create_worker_pane")
    @patch("dgov.experiment._emit_event")
    def test_error_on_timeout(self, mock_emit, mock_create, mock_wait, mock_close, tmp_path):
        from dgov.waiter import PaneTimeoutError

        mock_create.return_value = MagicMock(slug="exp-test04")
        mock_wait.side_effect = PaneTimeoutError("exp-test04", 600, "pi")

        result = run_experiment(
            project_root=str(tmp_path),
            program_text="Slow task",
            metric_name="val_bpb",
            metric_baseline=1.42,
            agent="pi",
            session_root=str(tmp_path),
            exp_id="exp-test04",
        )

        assert result["status"] == "error"
        assert result["error"] == "timeout"
        mock_close.assert_called_once()


# ---------------------------------------------------------------------------
# _compute_tiers (experiments are sequential = 1 per tier)
# ---------------------------------------------------------------------------


class TestExperimentSequential:
    """Experiments touching overlapping files land in separate tiers."""

    def test_overlapping_tasks_serialize(self) -> None:
        from dgov.batch import _compute_tiers

        tasks = [
            {"id": "exp-1", "touches": ["src/model.py"]},
            {"id": "exp-2", "touches": ["src/model.py"]},
        ]
        tiers = _compute_tiers(tasks)
        assert len(tiers) == 2
        assert tiers[0][0]["id"] == "exp-1"
        assert tiers[1][0]["id"] == "exp-2"


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------


class TestExperimentCLI:
    def test_dry_run(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from dgov.cli import cli

        program_file = tmp_path / "experiment.md"
        program_file.write_text("# Experiment\nTry something")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "experiment",
                "start",
                "--program",
                str(program_file),
                "--metric",
                "val_bpb",
                "--budget",
                "3",
                "--dry-run",
            ],
            env={"DGOV_SKIP_GOVERNOR_CHECK": "1"},
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["dry_run"] is True
        assert data["metric"] == "val_bpb"
        assert data["budget"] == 3

    def test_log_empty(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from dgov.cli import cli

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "experiment",
                "log",
                "--program",
                "nonexistent",
                "--project-root",
                str(tmp_path),
            ],
            env={"DGOV_SKIP_GOVERNOR_CHECK": "1"},
        )
        assert result.exit_code == 0
        assert json.loads(result.output) == []

    def test_summary_empty(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from dgov.cli import cli

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "experiment",
                "summary",
                "--program",
                "nonexistent",
                "--project-root",
                str(tmp_path),
            ],
            env={"DGOV_SKIP_GOVERNOR_CHECK": "1"},
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["total"] == 0
        assert data["best"] is None
