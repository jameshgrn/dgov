"""Tests for git status porcelain parsing."""

from __future__ import annotations

import pytest

from dgov.git_status import decode_porcelain_path, git_path_output_paths, porcelain_status_paths


@pytest.mark.unit
class TestDecodePorcelainPath:
    def test_decodes_quoted_spaces(self) -> None:
        assert decode_porcelain_path('"file with space.py"') == "file with space.py"

    def test_decodes_git_octal_utf8(self) -> None:
        assert decode_porcelain_path('"caf\\303\\251.py"') == "caf\u00e9.py"


@pytest.mark.unit
class TestPorcelainStatusPaths:
    def test_returns_changed_paths(self) -> None:
        output = '?? "file with space.py"\n M simple.py\n'

        assert porcelain_status_paths(output) == ("file with space.py", "simple.py")

    def test_handles_status_output_with_stripped_leading_space(self) -> None:
        output = "M .dgov/plans/deployed.jsonl\n"

        assert porcelain_status_paths(output) == (".dgov/plans/deployed.jsonl",)

    def test_rename_separator_inside_quoted_path(self) -> None:
        output = 'R  "old -> name.py" -> "new name.py"\n'

        assert porcelain_status_paths(output) == ("new name.py",)
        assert porcelain_status_paths(output, include_rename_sources=True) == (
            "old -> name.py",
            "new name.py",
        )

    def test_copy_source_is_not_returned_as_rename_source(self) -> None:
        output = "C  old.py -> new.py\n"

        assert porcelain_status_paths(output, include_rename_sources=True) == ("new.py",)

    def test_non_rename_path_with_arrow_is_not_split(self) -> None:
        output = " M notes/a -> b.txt\n"

        assert porcelain_status_paths(output, include_rename_sources=True) == ("notes/a -> b.txt",)


@pytest.mark.unit
class TestGitPathOutputPaths:
    def test_prefers_nul_delimited_paths(self) -> None:
        output = "leading space.py\0caf\u00e9.py\0"

        assert git_path_output_paths(output) == ("leading space.py", "caf\u00e9.py")

    def test_decodes_quoted_line_output(self) -> None:
        output = '"caf\\303\\251.py"\n'

        assert git_path_output_paths(output) == ("caf\u00e9.py",)
