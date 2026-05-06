"""Tests for sop_bundler — SOP loading, hashing, and prompt bundling."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from pytest_mock import MockerFixture

from dgov.plan import PlanUnit, PlanUnitFiles
from dgov.plan_tree import FlatPlan, RootMeta, merge_tree, resolve_refs, walk_tree
from dgov.sop_bundler import (
    BundleResult,
    IdentityBundler,
    Sop,
    SopBundler,
    TagBasedSopBundler,
    _normalize,
    bundle,
    compute_sop_set_hash,
    load_sops,
)

pytestmark = pytest.mark.unit


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _sop_md(
    name: str,
    title: str,
    do_item: str,
    *,
    summary: str = "Summary.",
    applies_to: tuple[str, ...] = ("general",),
    priority: str = "must",
    when: tuple[str, ...] = ("when it applies",),
    do_not: tuple[str, ...] = ("do not drift",),
    verify: tuple[str, ...] = ("verify the outcome",),
    escalate: tuple[str, ...] = ("escalate if scope changes",),
) -> str:
    applies_to_str = ", ".join(applies_to)
    when_body = "\n".join(f"- {item}" for item in when)
    do_not_body = "\n".join(f"- {item}" for item in do_not)
    verify_body = "\n".join(f"- {item}" for item in verify)
    escalate_body = "\n".join(f"- {item}" for item in escalate)
    return (
        "---\n"
        f"name: {name}\n"
        f"title: {title}\n"
        f"summary: {summary}\n"
        f"applies_to: [{applies_to_str}]\n"
        f"priority: {priority}\n"
        "---\n"
        "## When\n"
        f"{when_body}\n\n"
        "## Do\n"
        f"- {do_item}\n\n"
        "## Do Not\n"
        f"{do_not_body}\n\n"
        "## Verify\n"
        f"{verify_body}\n\n"
        "## Escalate\n"
        f"{escalate_body}\n"
    )


def _sop(
    name: str,
    title: str,
    *,
    summary: str = "Summary.",
    applies_to: tuple[str, ...] = ("general",),
    priority: str = "must",
    path: str = "s.md",
) -> Sop:
    return Sop(
        name=name,
        title=title,
        summary=summary,
        applies_to=applies_to,
        priority=priority,
        when=("when it applies",),
        do=("do it",),
        do_not=("do not drift",),
        verify=("verify the outcome",),
        escalate=("escalate if scope changes",),
        path=Path(path),
    )


def _rendered_block(
    title: str,
    *,
    summary: str = "Summary.",
    applies_to: tuple[str, ...] = ("general",),
    priority: str = "must",
    when: tuple[str, ...] = ("when it applies",),
    do: tuple[str, ...] = ("do it",),
    do_not: tuple[str, ...] = ("do not drift",),
    verify: tuple[str, ...] = ("verify the outcome",),
    escalate: tuple[str, ...] = ("escalate if scope changes",),
) -> str:
    lines = [
        f"[SOP: {title}]",
        f"Summary: {summary}",
        f"Applies To: {', '.join(applies_to)}",
        f"Priority: {priority.upper()}",
        "",
        "When:",
        *(f"- {item}" for item in when),
        "",
        "Do:",
        *(f"- {item}" for item in do),
        "",
        "Do Not:",
        *(f"- {item}" for item in do_not),
        "",
        "Verify:",
        *(f"- {item}" for item in verify),
        "",
        "Escalate:",
        *(f"- {item}" for item in escalate),
    ]
    return "\n".join(lines)


def _flat_plan(units: dict[str, PlanUnit] | None = None) -> FlatPlan:
    """Build a minimal FlatPlan for testing."""
    return FlatPlan(
        plan_root=Path("/fake"),
        root_meta=RootMeta(name="test", summary="t", sections=()),
        units=units or {},
        source_map={},
        source_mtime_max=0.0,
    )


def _unit(slug: str, prompt: str = "do the thing") -> PlanUnit:
    return PlanUnit(
        slug=slug,
        summary="s",
        prompt=prompt,
        commit_message="c",
        files=PlanUnitFiles(),
    )


# =============================================================================
# SOP parsing
# =============================================================================


class TestLoadSops:
    def test_empty_dir(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        sops_dir.mkdir()
        assert load_sops(sops_dir) == []

    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        assert load_sops(tmp_path / "nope") == []

    def test_parses_valid_sop(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(
            sops_dir / "testing.md",
            _sop_md(
                "testing",
                "Testing Guide",
                "Run pytest.",
                summary="Targeted test guidance.",
                applies_to=("tests", "pytest"),
            ),
        )
        sops = load_sops(sops_dir)
        assert len(sops) == 1
        assert sops[0].name == "testing"
        assert sops[0].title == "Testing Guide"
        assert sops[0].summary == "Targeted test guidance."
        assert sops[0].applies_to == ("tests", "pytest")
        assert sops[0].priority == "must"
        assert sops[0].do == ("Run pytest.",)

    def test_multiple_sops_sorted(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(sops_dir / "zulu.md", _sop_md("zulu", "Z", "z-body"))
        _write(sops_dir / "alpha.md", _sop_md("alpha", "A", "a-body"))
        sops = load_sops(sops_dir)
        assert [s.name for s in sops] == ["alpha", "zulu"]

    def test_invalid_without_front_matter(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(sops_dir / "bare.md", "Just markdown, no front matter.")
        with pytest.raises(ValueError, match="missing front matter"):
            load_sops(sops_dir)

    def test_invalid_missing_name(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(
            sops_dir / "no-name.md",
            "---\n"
            "title: Has Title\n"
            "summary: x\n"
            "applies_to: [general]\n"
            "priority: must\n"
            "---\n"
            "## When\n- x\n\n## Do\n- x\n\n## Do Not\n- x\n\n## Verify\n- x\n\n## Escalate\n- x\n",
        )
        with pytest.raises(ValueError, match="missing required front-matter field 'name'"):
            load_sops(sops_dir)

    def test_invalid_missing_summary(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(
            sops_dir / "no-summary.md",
            "---\n"
            "name: notitle\n"
            "title: Title\n"
            "applies_to: [general]\n"
            "priority: must\n"
            "---\n"
            "## When\n- x\n\n## Do\n- x\n\n## Do Not\n- x\n\n## Verify\n- x\n\n## Escalate\n- x\n",
        )
        with pytest.raises(ValueError, match="missing required front-matter field 'summary'"):
            load_sops(sops_dir)

    def test_ignores_non_md_files(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(sops_dir / "readme.txt", "not a sop")
        _write(sops_dir / "actual.md", _sop_md("actual", "A", "body"))
        sops = load_sops(sops_dir)
        assert len(sops) == 1
        assert sops[0].name == "actual"

    def test_invalid_missing_required_section(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(
            sops_dir / "ws.md",
            "---\n"
            "name: ws\n"
            "title: WS\n"
            "summary: x\n"
            "applies_to: [general]\n"
            "priority: must\n"
            "---\n"
            "## When\n- x\n\n## Do\n- x\n\n## Verify\n- x\n\n## Escalate\n- x\n",
        )
        with pytest.raises(ValueError, match="missing required sections: Do Not"):
            load_sops(sops_dir)

    def test_quoted_values(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(
            sops_dir / "q.md",
            "---\n"
            'name: "quoted-name"\n'
            "title: 'quoted-title'\n"
            'summary: "quoted summary"\n'
            'applies_to: ["alpha", "beta"]\n'
            "priority: 'should'\n"
            "---\n"
            "## When\n- quoted\n\n"
            "## Do\n- B\n\n"
            "## Do Not\n- no\n\n"
            "## Verify\n- yes\n\n"
            "## Escalate\n- maybe\n",
        )
        sops = load_sops(sops_dir)
        assert sops[0].name == "quoted-name"
        assert sops[0].title == "quoted-title"
        assert sops[0].summary == "quoted summary"
        assert sops[0].applies_to == ("alpha", "beta")
        assert sops[0].priority == "should"


# =============================================================================
# Hash
# =============================================================================


class TestSopSetHash:
    def test_deterministic(self, tmp_path: Path) -> None:
        sops = [
            _sop("a", "A", path=str(tmp_path / "a.md")),
            _sop("b", "B", path=str(tmp_path / "b.md")),
        ]
        assert compute_sop_set_hash(sops) == compute_sop_set_hash(sops)

    def test_order_independent(self, tmp_path: Path) -> None:
        s1 = _sop("a", "A", path=str(tmp_path / "a.md"))
        s2 = _sop("b", "B", path=str(tmp_path / "b.md"))
        assert compute_sop_set_hash([s1, s2]) == compute_sop_set_hash([s2, s1])

    def test_changes_on_title_change(self, tmp_path: Path) -> None:
        s1 = [_sop("a", "A", path=str(tmp_path / "a.md"))]
        s2 = [_sop("a", "Changed", path=str(tmp_path / "a.md"))]
        assert compute_sop_set_hash(s1) != compute_sop_set_hash(s2)

    def test_body_change_does_not_affect_hash(self, tmp_path: Path) -> None:
        s1 = [_sop("a", "A", path=str(tmp_path / "a.md"))]
        s2 = [
            Sop(
                name="a",
                title="A",
                summary="Summary.",
                applies_to=("general",),
                priority="must",
                when=("changed body",),
                do=("other",),
                do_not=("no",),
                verify=("yes",),
                escalate=("maybe",),
                path=tmp_path / "a.md",
            )
        ]
        assert compute_sop_set_hash(s1) == compute_sop_set_hash(s2)

    def test_summary_change_affects_hash(self, tmp_path: Path) -> None:
        s1 = [_sop("a", "A", summary="One", path=str(tmp_path / "a.md"))]
        s2 = [_sop("a", "A", summary="Two", path=str(tmp_path / "a.md"))]
        assert compute_sop_set_hash(s1) != compute_sop_set_hash(s2)


# =============================================================================
# Bundlers
# =============================================================================


class TestIdentityBundler:
    def test_returns_empty_mapping(self) -> None:
        units = {"a": _unit("a"), "b": _unit("b")}
        sops = [_sop("s", "S")]
        result = IdentityBundler().pick(units, sops)
        assert result == {"a": [], "b": []}


class TestTagBasedSopBundler:
    """Tests for the deterministic tag-intersection bundler."""

    def _unit_with(
        self,
        slug: str,
        summary: str = "s",
        *,
        files: PlanUnitFiles | None = None,
        role: Literal["worker", "researcher", "reviewer"] = "worker",
    ) -> PlanUnit:
        return PlanUnit(
            slug=slug,
            summary=summary,
            prompt="do the thing",
            commit_message="c",
            files=files or PlanUnitFiles(),
            role=role,
        )

    def test_matches_python_file_to_python_tag(self) -> None:
        unit = self._unit_with("a", files=PlanUnitFiles(edit=("src/foo.py",)))
        sops = [_sop("ps", "Python Style", applies_to=("python", "lint"))]
        result = TagBasedSopBundler().pick({"a": unit}, sops)
        assert result == {"a": ["ps"]}

    def test_matches_edited_test_file_to_testing_tag(self) -> None:
        unit = self._unit_with(
            "a",
            summary="Populate connector temporal metadata",
            files=PlanUnitFiles(edit=("tests/pressure_test/test_stac_connector.py",)),
        )
        sops = [
            _sop("testing", "Testing", applies_to=("tests",)),
            _sop("ps", "Python Style", applies_to=("python",)),
        ]

        result = TagBasedSopBundler().pick({"a": unit}, sops)

        assert sorted(result["a"]) == ["ps", "testing"]

    def test_matches_js_file_to_javascript_tag(self) -> None:
        unit = self._unit_with("a", files=PlanUnitFiles(create=("app.tsx",)))
        sops = [_sop("rv", "Return Values", applies_to=("javascript", "typescript"))]
        result = TagBasedSopBundler().pick({"a": unit}, sops)
        assert result == {"a": ["rv"]}

    def test_reviewer_role_matches_review_tag(self) -> None:
        unit = self._unit_with("a", role="reviewer")
        sops = [_sop("cr", "Code Review", applies_to=("review", "reviewer"))]
        result = TagBasedSopBundler().pick({"a": unit}, sops)
        assert result == {"a": ["cr"]}

    def test_researcher_role_matches_review_tag(self) -> None:
        unit = self._unit_with("a", role="researcher")
        sops = [_sop("cr", "Code Review", applies_to=("review",))]
        result = TagBasedSopBundler().pick({"a": unit}, sops)
        assert result == {"a": ["cr"]}

    def test_worker_role_does_not_match_review_tag(self) -> None:
        unit = self._unit_with("a", role="worker")
        sops = [_sop("cr", "Code Review", applies_to=("review", "reviewer"))]
        result = TagBasedSopBundler().pick({"a": unit}, sops)
        assert result == {"a": []}

    def test_summary_word_matches_tag(self) -> None:
        unit = self._unit_with("a", summary="Refactor the parser")
        sops = [_sop("rd", "Refactoring", applies_to=("refactor",))]
        result = TagBasedSopBundler().pick({"a": unit}, sops)
        assert result == {"a": ["rd"]}

    def test_summary_match_is_case_insensitive(self) -> None:
        unit = self._unit_with("a", summary="REFACTOR module")
        sops = [_sop("rd", "Refactoring", applies_to=("refactor",))]
        result = TagBasedSopBundler().pick({"a": unit}, sops)
        assert result == {"a": ["rd"]}

    def test_no_match_returns_empty(self) -> None:
        unit = self._unit_with("a", summary="Add logging")
        sops = [_sop("gc", "Git Commits", applies_to=("git", "commit"))]
        result = TagBasedSopBundler().pick({"a": unit}, sops)
        assert result == {"a": []}

    def test_empty_summary_only_uses_files_and_role(self) -> None:
        unit = self._unit_with("a", summary="", files=PlanUnitFiles(touch=("x.py",)))
        sops = [
            _sop("ps", "Python Style", applies_to=("python",)),
            _sop("gc", "Git Commits", applies_to=("git",)),
        ]
        result = TagBasedSopBundler().pick({"a": unit}, sops)
        assert result == {"a": ["ps"]}

    def test_multiple_sops_matched(self) -> None:
        unit = self._unit_with("a", summary="Refactor python module")
        sops = [
            _sop("ps", "Python Style", applies_to=("python",)),
            _sop("rd", "Refactoring", applies_to=("refactor",)),
            _sop("gc", "Git Commits", applies_to=("git",)),
        ]
        result = TagBasedSopBundler().pick({"a": unit}, sops)
        assert sorted(result["a"]) == ["ps", "rd"]

    def test_multiple_units(self) -> None:
        units = {
            "a": self._unit_with("a", summary="Fix git history"),
            "b": self._unit_with("b", summary="Add tests", files=PlanUnitFiles(edit=("t.py",))),
        }
        sops = [
            _sop("gc", "Git Commits", applies_to=("git",)),
            _sop("ts", "Testing", applies_to=("tests",)),
            _sop("ps", "Python Style", applies_to=("python",)),
        ]
        result = TagBasedSopBundler().pick(units, sops)
        assert result["a"] == ["gc"]
        assert sorted(result["b"]) == ["ps", "ts"]

    def test_punctuation_stripped_from_summary(self) -> None:
        unit = self._unit_with("a", summary="(refactor) cleanup.")
        sops = [_sop("rd", "Refactoring", applies_to=("refactor",))]
        result = TagBasedSopBundler().pick({"a": unit}, sops)
        assert result == {"a": ["rd"]}

    def test_no_files_no_crash(self) -> None:
        unit = PlanUnit(
            slug="a",
            summary="general task",
            prompt="do it",
            commit_message="c",
            files=PlanUnitFiles(),
        )
        sops = [_sop("ps", "Python Style", applies_to=("python",))]
        result = TagBasedSopBundler().pick({"a": unit}, sops)
        assert result == {"a": []}


class TestBundleCaching:
    def test_bundle_reuses_mapping_on_hash_match(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        sops_dir = tmp_path / "sops"
        _write(sops_dir / "s1.md", _sop_md("s1", "S1", "Body 1"))
        plan = _flat_plan({"a": _unit("a", "Prompt")})

        # Pre-calculated hash for s1.md
        hash_val = compute_sop_set_hash(load_sops(sops_dir))

        # Mock bundler — should NOT be called if cache hits
        bundler = mocker.Mock(spec=SopBundler)

        # 1. Cache hit
        cached_mapping: dict[str, tuple[str, ...]] = {"a": ("s1",)}
        result = bundle(
            plan, sops_dir, bundler, cached_mapping=cached_mapping, cached_hash=hash_val
        )
        assert result.sop_mapping == {"a": ("s1",)}
        expected = _rendered_block("S1", do=("Body 1",)) + "\n\nPrompt"
        assert result.plan.units["a"].prompt == expected
        bundler.pick.assert_not_called()

    def test_bundle_re_calls_on_hash_mismatch(self, tmp_path: Path, mocker: MockerFixture) -> None:
        sops_dir = tmp_path / "sops"
        _write(sops_dir / "s1.md", _sop_md("s1", "S1", "Body 1"))
        plan = _flat_plan({"a": _unit("a", "Prompt")})

        bundler = mocker.Mock(spec=SopBundler)
        bundler.pick.return_value = {"a": ["s1"]}

        # Mismatching hash
        result = bundle(
            plan, sops_dir, bundler, cached_mapping={"a": ("s1",)}, cached_hash="WRONG"
        )
        assert result.sop_mapping == {"a": ("s1",)}
        bundler.pick.assert_called_once()

    def test_bundle_re_calls_on_missing_unit_in_cache(
        self, tmp_path: Path, mocker: MockerFixture
    ) -> None:
        sops_dir = tmp_path / "sops"
        _write(sops_dir / "s1.md", _sop_md("s1", "S1", "Body 1"))
        plan = _flat_plan({"a": _unit("a"), "b": _unit("b")})
        hash_val = compute_sop_set_hash(load_sops(sops_dir))

        bundler = mocker.Mock(spec=SopBundler)
        bundler.pick.return_value = {"a": ["s1"], "b": []}

        # Mapping missing "b"
        result = bundle(
            plan, sops_dir, bundler, cached_mapping={"a": ("s1",)}, cached_hash=hash_val
        )
        assert result.sop_mapping == {"a": ("s1",), "b": ()}
        bundler.pick.assert_called_once()


class _PickAllBundler:
    """Test bundler that assigns all SOPs to every unit."""

    def pick(
        self,
        units: dict[str, PlanUnit],
        sops: list[Sop],
    ) -> dict[str, list[str]]:
        names = [s.name for s in sops]
        return {uid: list(names) for uid in units}


class _SelectiveBundler:
    """Test bundler that assigns specific SOPs per unit."""

    def __init__(self, mapping: dict[str, list[str]]) -> None:
        self._mapping = mapping

    def pick(
        self,
        units: dict[str, PlanUnit],
        sops: list[Sop],
    ) -> dict[str, list[str]]:
        return {uid: self._mapping.get(uid, []) for uid in units}


class TestBundleNoSops:
    def test_empty_sops_dir(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        sops_dir.mkdir()
        plan = _flat_plan({"a": _unit("a", "original")})
        result = bundle(plan, sops_dir, IdentityBundler())
        assert isinstance(result, BundleResult)
        assert result.sop_set_hash == ""
        assert result.sop_mapping == {"a": ()}
        assert result.plan.units["a"].prompt == "original"

    def test_nonexistent_sops_dir(self, tmp_path: Path) -> None:
        plan = _flat_plan({"a": _unit("a")})
        result = bundle(plan, tmp_path / "nope", IdentityBundler())
        assert result.sop_set_hash == ""
        assert result.plan.units["a"].prompt == "do the thing"


class TestBundleIdentity:
    def test_prompts_unchanged(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(sops_dir / "guide.md", _sop_md("guide", "Guide", "Guidance text."))
        plan = _flat_plan({"a": _unit("a", "original prompt")})
        result = bundle(plan, sops_dir, IdentityBundler())
        assert result.plan.units["a"].prompt == "original prompt"
        assert result.sop_mapping == {"a": ()}
        assert result.sop_set_hash != ""


class TestBundleRewrite:
    def test_single_sop_prepended(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(sops_dir / "style.md", _sop_md("style", "Style", "Use ruff."))
        plan = _flat_plan({"a": _unit("a", "Write code.")})
        result = bundle(plan, sops_dir, _PickAllBundler())
        assert (
            result.plan.units["a"].prompt
            == _rendered_block("Style", do=("Use ruff.",)) + "\n\nWrite code."
        )
        assert result.sop_mapping == {"a": ("style",)}

    def test_multiple_sops_concatenated(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(sops_dir / "a.md", _sop_md("alpha", "A", "Alpha body."))
        _write(sops_dir / "b.md", _sop_md("beta", "B", "Beta body."))
        plan = _flat_plan({"x": _unit("x", "Task prompt.")})
        result = bundle(plan, sops_dir, _PickAllBundler())
        assert result.plan.units["x"].prompt == (
            _rendered_block("A", do=("Alpha body.",))
            + "\n\n"
            + _rendered_block("B", do=("Beta body.",))
            + "\n\nTask prompt."
        )

    def test_selective_assignment(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(sops_dir / "lint.md", _sop_md("lint", "Lint", "Run linter."))
        _write(sops_dir / "test.md", _sop_md("test", "Test", "Run tests."))
        plan = _flat_plan({
            "a": _unit("a", "Task A."),
            "b": _unit("b", "Task B."),
        })
        mapping = {"a": ["lint"], "b": ["test"]}
        result = bundle(plan, sops_dir, _SelectiveBundler(mapping))
        expected_a = _rendered_block("Lint", do=("Run linter.",)) + "\n\nTask A."
        expected_b = _rendered_block("Test", do=("Run tests.",)) + "\n\nTask B."
        assert result.plan.units["a"].prompt == expected_a
        assert result.plan.units["b"].prompt == expected_b
        assert result.sop_mapping["a"] == ("lint",)
        assert result.sop_mapping["b"] == ("test",)

    def test_unknown_sop_name_ignored(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(sops_dir / "real.md", _sop_md("real", "Real", "Real body."))
        plan = _flat_plan({"a": _unit("a", "Original.")})
        result = bundle(plan, sops_dir, _SelectiveBundler({"a": ["nonexistent"]}))
        assert result.plan.units["a"].prompt == "Original."
        assert result.sop_mapping["a"] == ("nonexistent",)

    def test_unit_not_in_mapping_unchanged(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(sops_dir / "s.md", _sop_md("s", "S", "Body."))
        plan = _flat_plan({"a": _unit("a", "Prompt A."), "b": _unit("b", "Prompt B.")})
        result = bundle(plan, sops_dir, _SelectiveBundler({"a": ["s"]}))
        expected = _rendered_block("S", do=("Body.",)) + "\n\nPrompt A."
        assert result.plan.units["a"].prompt == expected
        assert result.plan.units["b"].prompt == "Prompt B."

    def test_hash_populated(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(sops_dir / "x.md", _sop_md("x", "X", "body"))
        plan = _flat_plan({"a": _unit("a")})
        result = bundle(plan, sops_dir, IdentityBundler())
        assert len(result.sop_set_hash) == 64  # SHA256 hex

    def test_non_unit_fields_preserved(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(sops_dir / "s.md", _sop_md("s", "S", "Prepend."))
        unit = PlanUnit(
            slug="a",
            summary="sum",
            prompt="original",
            commit_message="msg",
            files=PlanUnitFiles(edit=("file.py",)),
            depends_on=("dep",),
            agent="model",
            timeout_s=99,
        )
        plan = _flat_plan({"a": unit})
        result = bundle(plan, sops_dir, _PickAllBundler())
        u = result.plan.units["a"]
        assert u.summary == "sum"
        assert u.commit_message == "msg"
        assert u.files.edit == ("file.py",)
        assert u.depends_on == ("dep",)
        assert u.agent == "model"
        assert u.timeout_s == 99
        assert u.prompt.endswith("original")


class TestBundleIntegration:
    """Bundle against a real plan tree (plan-system dogfood)."""

    def test_bundle_plan_system_with_identity(self) -> None:
        project_root = Path(__file__).parent.parent
        plan_root = project_root / ".dgov" / "plans" / "archive" / "plan-system"
        sops_dir = project_root / ".dgov" / "sops"
        plan = resolve_refs(merge_tree(walk_tree(plan_root)))
        result = bundle(plan, sops_dir, IdentityBundler())
        # Identity bundler keeps prompts unchanged even when SOPs exist.
        for uid in plan.units:
            assert result.plan.units[uid].prompt == plan.units[uid].prompt
        assert result.sop_set_hash != ""


# =============================================================================
# Normalization
# =============================================================================


class TestNormalize:
    def test_strips_trailing_s(self) -> None:
        assert _normalize("tests") == "test"
        assert _normalize("errors") == "error"
        assert _normalize("types") == "type"

    def test_preserves_short_words(self) -> None:
        assert _normalize("bus") == "bus"
        assert _normalize("as") == "as"

    def test_preserves_double_s(self) -> None:
        assert _normalize("class") == "class"
        assert _normalize("pass") == "pass"

    def test_preserves_non_s_endings(self) -> None:
        assert _normalize("python") == "python"
        assert _normalize("refactor") == "refactor"


class TestTagNormalizationMatching:
    """Verify that plural normalization connects summaries to SOP tags."""

    def _unit_with(self, summary: str) -> PlanUnit:
        return PlanUnit(
            slug="a",
            summary=summary,
            prompt="p",
            commit_message="c",
            files=PlanUnitFiles(),
        )

    def test_plural_summary_matches_singular_tag(self) -> None:
        """'tests' in summary matches SOP with 'test' tag."""
        unit = self._unit_with("Update tests for module")
        sops = [_sop("ts", "Testing", applies_to=("test",))]
        result = TagBasedSopBundler().pick({"a": unit}, sops)
        assert result == {"a": ["ts"]}

    def test_singular_summary_matches_plural_tag(self) -> None:
        """'error' in summary matches SOP with 'errors' tag."""
        unit = self._unit_with("Fix error handling")
        sops = [_sop("eh", "Error Handling", applies_to=("errors",))]
        result = TagBasedSopBundler().pick({"a": unit}, sops)
        assert result == {"a": ["eh"]}

    def test_plural_tag_matches_singular_summary(self) -> None:
        """SOP with 'exceptions' tag matches summary containing 'exception'."""
        unit = self._unit_with("Handle exception in parser")
        sops = [_sop("eh", "Error Handling", applies_to=("exceptions",))]
        result = TagBasedSopBundler().pick({"a": unit}, sops)
        assert result == {"a": ["eh"]}
