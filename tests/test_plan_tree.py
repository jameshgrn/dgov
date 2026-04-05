"""Tests for plan_tree walker + merger + resolver."""

from __future__ import annotations

from pathlib import Path

import pytest

from dgov.plan_tree import FlatPlan, merge_tree, resolve_refs, walk_tree

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


# =============================================================================
# merger tests
# =============================================================================


_TASK_STUB = """
[tasks.{slug}]
summary = "s"
prompt = "p"
commit_message = "c"
"""


def _build_tree(tmp_path: Path, sections: list[str], files: dict[str, str]) -> Path:
    """Create a plan tree from a dict of {relative_path: content}."""
    sections_toml = "[" + ", ".join(f'"{s}"' for s in sections) + "]"
    _write(
        tmp_path / "_root.toml",
        f'[plan]\nname = "test"\nsummary = "t"\nsections = {sections_toml}\n',
    )
    for s in sections:
        (tmp_path / s).mkdir(exist_ok=True)
    for rel_path, content in files.items():
        _write(tmp_path / rel_path, content)
    return tmp_path


class TestMergerHappyPath:
    def test_single_task_single_file(self, tmp_path: Path) -> None:
        _build_tree(tmp_path, ["alpha"], {"alpha/one.toml": _TASK_STUB.format(slug="foo")})
        plan = merge_tree(walk_tree(tmp_path))
        assert isinstance(plan, FlatPlan)
        assert set(plan.units.keys()) == {"alpha/one.foo"}
        unit = plan.units["alpha/one.foo"]
        assert unit.slug == "alpha/one.foo"
        assert unit.summary == "s"
        assert unit.prompt == "p"
        assert unit.commit_message == "c"

    def test_multiple_tasks_per_file(self, tmp_path: Path) -> None:
        content = _TASK_STUB.format(slug="foo") + _TASK_STUB.format(slug="bar")
        _build_tree(tmp_path, ["alpha"], {"alpha/one.toml": content})
        plan = merge_tree(walk_tree(tmp_path))
        assert set(plan.units.keys()) == {"alpha/one.foo", "alpha/one.bar"}

    def test_multiple_files_per_section(self, tmp_path: Path) -> None:
        _build_tree(
            tmp_path,
            ["alpha"],
            {
                "alpha/one.toml": _TASK_STUB.format(slug="foo"),
                "alpha/two.toml": _TASK_STUB.format(slug="bar"),
            },
        )
        plan = merge_tree(walk_tree(tmp_path))
        assert set(plan.units.keys()) == {"alpha/one.foo", "alpha/two.bar"}

    def test_multiple_sections(self, tmp_path: Path) -> None:
        _build_tree(
            tmp_path,
            ["alpha", "beta"],
            {
                "alpha/x.toml": _TASK_STUB.format(slug="a"),
                "beta/y.toml": _TASK_STUB.format(slug="b"),
            },
        )
        plan = merge_tree(walk_tree(tmp_path))
        assert set(plan.units.keys()) == {"alpha/x.a", "beta/y.b"}

    def test_cross_file_slug_collisions_ok(self, tmp_path: Path) -> None:
        """Same bare slug in different files is fine — path qualification."""
        _build_tree(
            tmp_path,
            ["alpha"],
            {
                "alpha/one.toml": _TASK_STUB.format(slug="foo"),
                "alpha/two.toml": _TASK_STUB.format(slug="foo"),
            },
        )
        plan = merge_tree(walk_tree(tmp_path))
        assert set(plan.units.keys()) == {"alpha/one.foo", "alpha/two.foo"}

    def test_source_map_populated(self, tmp_path: Path) -> None:
        _build_tree(tmp_path, ["alpha"], {"alpha/one.toml": _TASK_STUB.format(slug="foo")})
        plan = merge_tree(walk_tree(tmp_path))
        assert plan.source_map["alpha/one.foo"] == tmp_path / "alpha" / "one.toml"

    def test_source_mtime_max_tracked(self, tmp_path: Path) -> None:
        _build_tree(tmp_path, ["alpha"], {"alpha/one.toml": _TASK_STUB.format(slug="foo")})
        plan = merge_tree(walk_tree(tmp_path))
        expected = (tmp_path / "alpha" / "one.toml").stat().st_mtime
        assert plan.source_mtime_max == expected

    def test_root_meta_carried_through(self, tmp_path: Path) -> None:
        _build_tree(tmp_path, ["alpha"], {"alpha/one.toml": _TASK_STUB.format(slug="foo")})
        plan = merge_tree(walk_tree(tmp_path))
        assert plan.root_meta.name == "test"
        assert plan.root_meta.sections == ("alpha",)

    def test_preserves_all_unit_fields(self, tmp_path: Path) -> None:
        content = """
[tasks.full]
summary = "sum"
prompt = "prm"
commit_message = "msg"
agent = "acct/model"
depends_on = ["a", "b/c.d"]
timeout_s = 123
files.create = ["new.py"]
files.edit = ["old.py"]
files.delete = ["gone.py"]
"""
        _build_tree(tmp_path, ["alpha"], {"alpha/one.toml": content})
        plan = merge_tree(walk_tree(tmp_path))
        unit = plan.units["alpha/one.full"]
        assert unit.summary == "sum"
        assert unit.prompt == "prm"
        assert unit.commit_message == "msg"
        assert unit.agent == "acct/model"
        assert unit.depends_on == ("a", "b/c.d")
        assert unit.timeout_s == 123
        assert unit.files.create == ("new.py",)
        assert unit.files.edit == ("old.py",)
        assert unit.files.delete == ("gone.py",)

    def test_defaults_for_missing_fields(self, tmp_path: Path) -> None:
        content = '[tasks.minimal]\nsummary = "s"\nprompt = "p"\ncommit_message = "c"\n'
        _build_tree(tmp_path, ["alpha"], {"alpha/one.toml": content})
        plan = merge_tree(walk_tree(tmp_path))
        unit = plan.units["alpha/one.minimal"]
        assert unit.agent == ""
        assert unit.depends_on == ()
        assert unit.timeout_s == 0
        assert unit.files.create == ()

    def test_file_with_no_tasks_section(self, tmp_path: Path) -> None:
        _build_tree(tmp_path, ["alpha"], {"alpha/empty.toml": "# just a comment\n"})
        plan = merge_tree(walk_tree(tmp_path))
        assert plan.units == {}


class TestMergerSlugGrammar:
    @pytest.mark.parametrize("slug", ["foo.bar", "foo/bar", "foo bar", "foo!", "foo$", ""])
    def test_invalid_slug_rejected(self, tmp_path: Path, slug: str) -> None:
        content = f'[tasks."{slug}"]\nsummary = "s"\nprompt = "p"\ncommit_message = "c"\n'
        _build_tree(tmp_path, ["alpha"], {"alpha/one.toml": content})
        with pytest.raises(ValueError, match="Invalid slug"):
            merge_tree(walk_tree(tmp_path))

    @pytest.mark.parametrize("slug", ["foo", "foo-bar", "foo_bar", "a1b2", "FOO", "x", "A_B-C_1"])
    def test_valid_slug_accepted(self, tmp_path: Path, slug: str) -> None:
        content = f'[tasks.{slug}]\nsummary = "s"\nprompt = "p"\ncommit_message = "c"\n'
        _build_tree(tmp_path, ["alpha"], {"alpha/one.toml": content})
        plan = merge_tree(walk_tree(tmp_path))
        assert f"alpha/one.{slug}" in plan.units


class TestMergerTypeChecks:
    def test_depends_on_not_list(self, tmp_path: Path) -> None:
        content = (
            '[tasks.foo]\nsummary = "s"\nprompt = "p"\n'
            'commit_message = "c"\ndepends_on = "single-string"\n'
        )
        _build_tree(tmp_path, ["alpha"], {"alpha/one.toml": content})
        with pytest.raises(ValueError, match="depends_on must be a list"):
            merge_tree(walk_tree(tmp_path))

    def test_depends_on_mixed_types(self, tmp_path: Path) -> None:
        content = (
            '[tasks.foo]\nsummary = "s"\nprompt = "p"\n'
            'commit_message = "c"\ndepends_on = ["a", 1]\n'
        )
        _build_tree(tmp_path, ["alpha"], {"alpha/one.toml": content})
        with pytest.raises(ValueError, match="depends_on must be a list"):
            merge_tree(walk_tree(tmp_path))

    def test_files_create_not_list(self, tmp_path: Path) -> None:
        content = (
            '[tasks.foo]\nsummary = "s"\nprompt = "p"\n'
            'commit_message = "c"\n[tasks.foo.files]\ncreate = "nope"\n'
        )
        _build_tree(tmp_path, ["alpha"], {"alpha/one.toml": content})
        with pytest.raises(ValueError, match="files.create must be a list"):
            merge_tree(walk_tree(tmp_path))


class TestMergerDogfood:
    def test_merges_plan_system_tree(self) -> None:
        plan_root = Path(__file__).parent.parent / ".dgov" / "plans" / "plan-system"
        plan = merge_tree(walk_tree(plan_root))
        expected_ids = {
            "compile/pipeline.walker",
            "compile/pipeline.merger",
            "compile/pipeline.resolver",
            "compile/pipeline.validator",
            "compile/pipeline.sop-bundler",
            "cli/compile-cmd.compile-cmd",
            "cli/status-cmd.status-cmd",
            "runtime/deploy-log.deploy-log",
        }
        assert set(plan.units.keys()) == expected_ids
        assert plan.source_mtime_max > 0.0


# =============================================================================
# resolver tests
# =============================================================================


def _unit_toml(slug: str, depends_on: list[str] | None = None) -> str:
    """Build a minimal [tasks.<slug>] block with optional depends_on."""
    deps = ""
    if depends_on is not None:
        deps_str = "[" + ", ".join(f'"{d}"' for d in depends_on) + "]"
        deps = f"depends_on = {deps_str}\n"
    return f'[tasks.{slug}]\nsummary = "s"\nprompt = "p"\ncommit_message = "c"\n{deps}'


def _resolve(tmp_path: Path, sections: list[str], files: dict[str, str]) -> FlatPlan:
    _build_tree(tmp_path, sections, files)
    return resolve_refs(merge_tree(walk_tree(tmp_path)))


class TestResolverHappyPath:
    def test_bare_ref_resolves_to_same_file(self, tmp_path: Path) -> None:
        content = _unit_toml("foo") + _unit_toml("bar", depends_on=["foo"])
        plan = _resolve(tmp_path, ["alpha"], {"alpha/one.toml": content})
        assert plan.units["alpha/one.bar"].depends_on == ("alpha/one.foo",)

    def test_path_qualified_ref_resolves_directly(self, tmp_path: Path) -> None:
        plan = _resolve(
            tmp_path,
            ["alpha"],
            {
                "alpha/one.toml": _unit_toml("foo"),
                "alpha/two.toml": _unit_toml("bar", depends_on=["alpha/one.foo"]),
            },
        )
        assert plan.units["alpha/two.bar"].depends_on == ("alpha/one.foo",)

    def test_mixed_bare_and_qualified(self, tmp_path: Path) -> None:
        plan = _resolve(
            tmp_path,
            ["alpha"],
            {
                "alpha/one.toml": _unit_toml("a") + _unit_toml("b"),
                "alpha/two.toml": (
                    _unit_toml("x", depends_on=["alpha/one.a", "y"]) + _unit_toml("y")
                ),
            },
        )
        assert plan.units["alpha/two.x"].depends_on == ("alpha/one.a", "alpha/two.y")

    def test_forward_ref_within_file_allowed(self, tmp_path: Path) -> None:
        content = _unit_toml("bar", depends_on=["foo"]) + _unit_toml("foo")
        plan = _resolve(tmp_path, ["alpha"], {"alpha/one.toml": content})
        assert plan.units["alpha/one.bar"].depends_on == ("alpha/one.foo",)

    def test_empty_depends_on_preserved(self, tmp_path: Path) -> None:
        plan = _resolve(tmp_path, ["alpha"], {"alpha/one.toml": _unit_toml("foo")})
        assert plan.units["alpha/one.foo"].depends_on == ()

    def test_non_depends_on_fields_preserved(self, tmp_path: Path) -> None:
        content = (
            '[tasks.foo]\nsummary = "S"\nprompt = "P"\ncommit_message = "C"\n'
            'agent = "a"\ntimeout_s = 42\n' + _unit_toml("bar", depends_on=["foo"])
        )
        plan = _resolve(tmp_path, ["alpha"], {"alpha/one.toml": content})
        foo = plan.units["alpha/one.foo"]
        assert foo.summary == "S"
        assert foo.prompt == "P"
        assert foo.commit_message == "C"
        assert foo.agent == "a"
        assert foo.timeout_s == 42

    def test_multiple_deps(self, tmp_path: Path) -> None:
        content = _unit_toml("a") + _unit_toml("b") + _unit_toml("c", depends_on=["a", "b"])
        plan = _resolve(tmp_path, ["alpha"], {"alpha/one.toml": content})
        assert plan.units["alpha/one.c"].depends_on == ("alpha/one.a", "alpha/one.b")

    def test_cross_section_path_qualified(self, tmp_path: Path) -> None:
        plan = _resolve(
            tmp_path,
            ["alpha", "beta"],
            {
                "alpha/x.toml": _unit_toml("a"),
                "beta/y.toml": _unit_toml("b", depends_on=["alpha/x.a"]),
            },
        )
        assert plan.units["beta/y.b"].depends_on == ("alpha/x.a",)


class TestResolverSelfReference:
    def test_bare_self_ref(self, tmp_path: Path) -> None:
        _build_tree(
            tmp_path,
            ["alpha"],
            {"alpha/one.toml": _unit_toml("foo", depends_on=["foo"])},
        )
        with pytest.raises(ValueError, match="Self-reference"):
            resolve_refs(merge_tree(walk_tree(tmp_path)))

    def test_path_qualified_self_ref(self, tmp_path: Path) -> None:
        _build_tree(
            tmp_path,
            ["alpha"],
            {"alpha/one.toml": _unit_toml("foo", depends_on=["alpha/one.foo"])},
        )
        with pytest.raises(ValueError, match="Self-reference"):
            resolve_refs(merge_tree(walk_tree(tmp_path)))


class TestResolverUnknownRefs:
    def test_unknown_bare_ref_no_hint(self, tmp_path: Path) -> None:
        _build_tree(
            tmp_path,
            ["alpha"],
            {"alpha/one.toml": _unit_toml("foo", depends_on=["completely-different"])},
        )
        with pytest.raises(ValueError, match="Unknown bare ref 'completely-different'"):
            resolve_refs(merge_tree(walk_tree(tmp_path)))

    def test_unknown_bare_ref_same_file_hint(self, tmp_path: Path) -> None:
        content = _unit_toml("migrate-worker") + _unit_toml("x", depends_on=["migrate-walker"])
        _build_tree(tmp_path, ["alpha"], {"alpha/one.toml": content})
        with pytest.raises(ValueError, match="did you mean 'migrate-worker'"):
            resolve_refs(merge_tree(walk_tree(tmp_path)))

    def test_unknown_bare_ref_cross_file_hint(self, tmp_path: Path) -> None:
        """Bare ref with no same-file match but unique cross-file match → hint."""
        _build_tree(
            tmp_path,
            ["alpha"],
            {
                "alpha/one.toml": _unit_toml("uniq"),
                "alpha/two.toml": _unit_toml("caller", depends_on=["uniq"]),
            },
        )
        with pytest.raises(ValueError, match="did you mean 'alpha/one.uniq'"):
            resolve_refs(merge_tree(walk_tree(tmp_path)))

    def test_unknown_bare_ref_ambiguous_cross_file_no_hint(self, tmp_path: Path) -> None:
        """If bare slug matches multiple cross-file units, no hint (ambiguous)."""
        _build_tree(
            tmp_path,
            ["alpha"],
            {
                "alpha/one.toml": _unit_toml("shared"),
                "alpha/two.toml": _unit_toml("shared"),
                "alpha/three.toml": _unit_toml("caller", depends_on=["shared"]),
            },
        )
        with pytest.raises(ValueError) as excinfo:
            resolve_refs(merge_tree(walk_tree(tmp_path)))
        assert "did you mean" not in str(excinfo.value)

    def test_unknown_path_qualified_ref(self, tmp_path: Path) -> None:
        _build_tree(
            tmp_path,
            ["alpha"],
            {
                "alpha/one.toml": _unit_toml("foo"),
                "alpha/two.toml": _unit_toml("bar", depends_on=["alpha/one.does-not-exist"]),
            },
        )
        with pytest.raises(ValueError, match="Unknown ref 'alpha/one.does-not-exist'"):
            resolve_refs(merge_tree(walk_tree(tmp_path)))

    def test_unknown_path_qualified_ref_with_hint(self, tmp_path: Path) -> None:
        """Close typo on path-qualified ref → 'did you mean' hint."""
        _build_tree(
            tmp_path,
            ["alpha"],
            {
                "alpha/one.toml": _unit_toml("builder"),
                "alpha/two.toml": _unit_toml("x", depends_on=["alpha/one.bilder"]),
            },
        )
        with pytest.raises(ValueError, match="did you mean 'alpha/one.builder'"):
            resolve_refs(merge_tree(walk_tree(tmp_path)))


class TestResolverDogfood:
    def test_resolves_plan_system_refs(self) -> None:
        plan_root = Path(__file__).parent.parent / ".dgov" / "plans" / "plan-system"
        plan = resolve_refs(merge_tree(walk_tree(plan_root)))
        # compile pipeline chain: walker → merger → resolver → validator → sop-bundler
        assert plan.units["compile/pipeline.merger"].depends_on == ("compile/pipeline.walker",)
        assert plan.units["compile/pipeline.validator"].depends_on == (
            "compile/pipeline.resolver",
        )
        # compile-cmd depends on two cross-file qualified refs
        compile_cmd_deps = set(plan.units["cli/compile-cmd.compile-cmd"].depends_on)
        assert compile_cmd_deps == {
            "compile/pipeline.validator",
            "compile/pipeline.sop-bundler",
        }
        # status-cmd depends on two cross-file qualified refs
        status_deps = set(plan.units["cli/status-cmd.status-cmd"].depends_on)
        assert status_deps == {
            "cli/compile-cmd.compile-cmd",
            "runtime/deploy-log.deploy-log",
        }
