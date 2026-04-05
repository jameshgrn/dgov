"""Tests for plan_tree walker."""

from __future__ import annotations

from pathlib import Path

import pytest

from dgov.plan_tree import walk_tree

pytestmark = pytest.mark.unit


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _minimal_root(tmp_path: Path, sections: str = '["alpha"]') -> None:
    _write(
        tmp_path / "_root.toml",
        f'[plan]\nname = "test"\nsummary = "test plan"\nsections = {sections}\n',
    )


class TestHappyPath:
    def test_parses_root_meta(self, tmp_path: Path) -> None:
        _minimal_root(tmp_path, '["alpha"]')
        (tmp_path / "alpha").mkdir()
        tree = walk_tree(tmp_path)
        assert tree.root_meta.name == "test"
        assert tree.root_meta.summary == "test plan"
        assert tree.root_meta.sections == ("alpha",)

    def test_preserves_section_order(self, tmp_path: Path) -> None:
        _minimal_root(tmp_path, '["charlie", "alpha", "bravo"]')
        for s in ("charlie", "alpha", "bravo"):
            (tmp_path / s).mkdir()
        tree = walk_tree(tmp_path)
        assert tree.root_meta.sections == ("charlie", "alpha", "bravo")

    def test_collects_toml_files_sorted(self, tmp_path: Path) -> None:
        _minimal_root(tmp_path, '["alpha"]')
        _write(tmp_path / "alpha" / "zeta.toml", "[tasks.x]\n")
        _write(tmp_path / "alpha" / "alpha.toml", "[tasks.y]\n")
        _write(tmp_path / "alpha" / "mike.toml", "[tasks.z]\n")
        tree = walk_tree(tmp_path)
        names = [p.name for p in tree.section_files["alpha"]]
        assert names == ["alpha.toml", "mike.toml", "zeta.toml"]

    def test_exposes_plan_root(self, tmp_path: Path) -> None:
        _minimal_root(tmp_path)
        (tmp_path / "alpha").mkdir()
        tree = walk_tree(tmp_path)
        assert tree.plan_root == tmp_path

    def test_summary_defaults_to_empty(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "_root.toml",
            '[plan]\nname = "test"\nsections = []\n',
        )
        tree = walk_tree(tmp_path)
        assert tree.root_meta.summary == ""


class TestSkipping:
    def test_skips_hidden_files(self, tmp_path: Path) -> None:
        _minimal_root(tmp_path)
        (tmp_path / "alpha").mkdir()
        _write(tmp_path / "alpha" / ".hidden.toml", "")
        _write(tmp_path / "alpha" / "visible.toml", "[tasks.x]\n")
        tree = walk_tree(tmp_path)
        names = [p.name for p in tree.section_files["alpha"]]
        assert names == ["visible.toml"]

    def test_skips_underscore_prefixed(self, tmp_path: Path) -> None:
        _minimal_root(tmp_path)
        (tmp_path / "alpha").mkdir()
        _write(tmp_path / "alpha" / "_draft.toml", "")
        _write(tmp_path / "alpha" / "_compiled.toml", "")
        _write(tmp_path / "alpha" / "real.toml", "[tasks.x]\n")
        tree = walk_tree(tmp_path)
        names = [p.name for p in tree.section_files["alpha"]]
        assert names == ["real.toml"]

    def test_ignores_non_toml_files(self, tmp_path: Path) -> None:
        _minimal_root(tmp_path)
        (tmp_path / "alpha").mkdir()
        _write(tmp_path / "alpha" / "README.md", "")
        _write(tmp_path / "alpha" / "notes.txt", "")
        _write(tmp_path / "alpha" / "foo.toml", "[tasks.x]\n")
        tree = walk_tree(tmp_path)
        names = [p.name for p in tree.section_files["alpha"]]
        assert names == ["foo.toml"]

    def test_ignores_subdirectories(self, tmp_path: Path) -> None:
        _minimal_root(tmp_path)
        (tmp_path / "alpha" / "nested").mkdir(parents=True)
        _write(tmp_path / "alpha" / "nested" / "child.toml", "[tasks.x]\n")
        _write(tmp_path / "alpha" / "top.toml", "[tasks.y]\n")
        tree = walk_tree(tmp_path)
        names = [p.name for p in tree.section_files["alpha"]]
        assert names == ["top.toml"]

    def test_ignores_undeclared_directories(self, tmp_path: Path) -> None:
        _minimal_root(tmp_path, '["alpha"]')
        (tmp_path / "alpha").mkdir()
        (tmp_path / "beta").mkdir()
        _write(tmp_path / "beta" / "orphan.toml", "[tasks.x]\n")
        tree = walk_tree(tmp_path)
        assert "beta" not in tree.section_files
        assert list(tree.section_files.keys()) == ["alpha"]


class TestErrors:
    def test_missing_root_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="_root.toml"):
            walk_tree(tmp_path)

    def test_missing_plan_section(self, tmp_path: Path) -> None:
        _write(tmp_path / "_root.toml", "[other]\nkey = 1\n")
        with pytest.raises(ValueError, match=r"missing \[plan\] section"):
            walk_tree(tmp_path)

    def test_missing_name(self, tmp_path: Path) -> None:
        _write(tmp_path / "_root.toml", '[plan]\nsummary = "x"\nsections = []\n')
        with pytest.raises(ValueError, match="missing 'name'"):
            walk_tree(tmp_path)

    def test_empty_name_rejected(self, tmp_path: Path) -> None:
        _write(tmp_path / "_root.toml", '[plan]\nname = ""\nsections = []\n')
        with pytest.raises(ValueError, match="missing 'name'"):
            walk_tree(tmp_path)

    def test_sections_not_list(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "_root.toml",
            '[plan]\nname = "x"\nsummary = "y"\nsections = "nope"\n',
        )
        with pytest.raises(ValueError, match="must be a list"):
            walk_tree(tmp_path)

    def test_sections_with_non_string_entry(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "_root.toml",
            '[plan]\nname = "x"\nsections = ["alpha", 1, "bravo"]\n',
        )
        with pytest.raises(ValueError, match="only strings"):
            walk_tree(tmp_path)

    def test_declared_section_missing_dir(self, tmp_path: Path) -> None:
        _minimal_root(tmp_path, '["alpha", "missing"]')
        (tmp_path / "alpha").mkdir()
        with pytest.raises(ValueError, match="'missing' has no directory"):
            walk_tree(tmp_path)

    def test_declared_section_is_file_not_dir(self, tmp_path: Path) -> None:
        _minimal_root(tmp_path, '["alpha"]')
        _write(tmp_path / "alpha", "not a directory")
        with pytest.raises(ValueError, match="'alpha' has no directory"):
            walk_tree(tmp_path)


class TestEmptyStates:
    def test_empty_sections_list(self, tmp_path: Path) -> None:
        _minimal_root(tmp_path, "[]")
        tree = walk_tree(tmp_path)
        assert tree.section_files == {}
        assert tree.root_meta.sections == ()

    def test_empty_section_directory(self, tmp_path: Path) -> None:
        _minimal_root(tmp_path, '["alpha"]')
        (tmp_path / "alpha").mkdir()
        tree = walk_tree(tmp_path)
        assert tree.section_files["alpha"] == ()


class TestDogfood:
    """Smoke test: walker can parse the plan-system's own tree."""

    def test_walks_plan_system_tree(self) -> None:
        plan_root = Path(__file__).parent.parent / ".dgov" / "plans" / "plan-system"
        tree = walk_tree(plan_root)
        assert tree.root_meta.name == "plan-system"
        assert set(tree.root_meta.sections) == {"compile", "cli", "runtime"}
        # Current plan-system tree contents (as of this commit)
        assert len(tree.section_files["compile"]) == 1
        assert len(tree.section_files["cli"]) == 2
        assert len(tree.section_files["runtime"]) == 1
