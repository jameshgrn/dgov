"""Tests for sop_bundler — SOP loading, hashing, and prompt bundling."""

from __future__ import annotations

from pathlib import Path

import pytest

from dgov.plan import PlanUnit, PlanUnitFiles
from dgov.plan_tree import FlatPlan, RootMeta, merge_tree, resolve_refs, walk_tree
from dgov.sop_bundler import (
    BundleResult,
    IdentityBundler,
    LLMSopBundler,
    Sop,
    SopBundler,
    bundle,
    compute_sop_set_hash,
    load_sops,
)

pytestmark = pytest.mark.unit


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _sop_md(name: str, title: str, body: str) -> str:
    return f"---\nname: {name}\ntitle: {title}\n---\n{body}\n"


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
        _write(sops_dir / "testing.md", _sop_md("testing", "Testing Guide", "Run pytest."))
        sops = load_sops(sops_dir)
        assert len(sops) == 1
        assert sops[0].name == "testing"
        assert sops[0].title == "Testing Guide"
        assert sops[0].body == "Run pytest."

    def test_multiple_sops_sorted(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(sops_dir / "zulu.md", _sop_md("zulu", "Z", "z-body"))
        _write(sops_dir / "alpha.md", _sop_md("alpha", "A", "a-body"))
        sops = load_sops(sops_dir)
        assert [s.name for s in sops] == ["alpha", "zulu"]

    def test_skips_no_front_matter(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(sops_dir / "bare.md", "Just markdown, no front matter.")
        assert load_sops(sops_dir) == []

    def test_skips_missing_name(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(sops_dir / "no-name.md", "---\ntitle: Has Title\n---\nBody.\n")
        assert load_sops(sops_dir) == []

    def test_title_defaults_to_empty(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(sops_dir / "notitle.md", "---\nname: notitle\n---\nBody.\n")
        sops = load_sops(sops_dir)
        assert sops[0].title == ""

    def test_ignores_non_md_files(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(sops_dir / "readme.txt", "not a sop")
        _write(sops_dir / "actual.md", _sop_md("actual", "A", "body"))
        sops = load_sops(sops_dir)
        assert len(sops) == 1
        assert sops[0].name == "actual"

    def test_body_strips_whitespace(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(sops_dir / "ws.md", "---\nname: ws\ntitle: WS\n---\n\n  body  \n\n")
        sops = load_sops(sops_dir)
        assert sops[0].body == "body"

    def test_quoted_values(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(sops_dir / "q.md", "---\nname: \"quoted-name\"\ntitle: 'quoted-title'\n---\nB\n")
        sops = load_sops(sops_dir)
        assert sops[0].name == "quoted-name"
        assert sops[0].title == "quoted-title"


# =============================================================================
# Hash
# =============================================================================


class TestSopSetHash:
    def test_deterministic(self, tmp_path: Path) -> None:
        sops = [
            Sop(name="a", title="A", body="", path=tmp_path / "a.md"),
            Sop(name="b", title="B", body="", path=tmp_path / "b.md"),
        ]
        assert compute_sop_set_hash(sops) == compute_sop_set_hash(sops)

    def test_order_independent(self, tmp_path: Path) -> None:
        s1 = Sop(name="a", title="A", body="", path=tmp_path / "a.md")
        s2 = Sop(name="b", title="B", body="", path=tmp_path / "b.md")
        assert compute_sop_set_hash([s1, s2]) == compute_sop_set_hash([s2, s1])

    def test_changes_on_title_change(self, tmp_path: Path) -> None:
        s1 = [Sop(name="a", title="A", body="x", path=tmp_path / "a.md")]
        s2 = [Sop(name="a", title="Changed", body="x", path=tmp_path / "a.md")]
        assert compute_sop_set_hash(s1) != compute_sop_set_hash(s2)

    def test_body_change_does_not_affect_hash(self, tmp_path: Path) -> None:
        s1 = [Sop(name="a", title="A", body="old body", path=tmp_path / "a.md")]
        s2 = [Sop(name="a", title="A", body="new body", path=tmp_path / "a.md")]
        assert compute_sop_set_hash(s1) == compute_sop_set_hash(s2)


# =============================================================================
# Bundlers
# =============================================================================


class TestIdentityBundler:
    def test_returns_empty_mapping(self) -> None:
        units = {"a": _unit("a"), "b": _unit("b")}
        sops = [Sop(name="s", title="S", body="", path=Path("s.md"))]
        result = IdentityBundler().pick(units, sops)
        assert result == {"a": [], "b": []}


class TestLLMSopBundler:
    def test_requires_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
        with pytest.raises(ValueError, match="FIREWORKS_API_KEY missing"):
            LLMSopBundler().pick({}, [])

    def test_successful_pick(self, monkeypatch: pytest.MonkeyPatch, mocker: pytest_mock.MockerFixture) -> None:
        monkeypatch.setenv("FIREWORKS_API_KEY", "fake")
        mock_client = mocker.patch("dgov.sop_bundler.OpenAI")
        mock_resp = mocker.MagicMock()
        mock_resp.choices[0].message.content = '{"mapping": {"a": ["s1"], "b": []}}'
        mock_client.return_value.chat.completions.create.return_value = mock_resp

        units = {"a": _unit("a"), "b": _unit("b")}
        sops = [Sop(name="s1", title="S1", body="", path=Path("s1.md"))]

        result = LLMSopBundler().pick(units, sops)
        assert result == {"a": ["s1"], "b": []}

        # Verify prompt contents
        _, kwargs = mock_client.return_value.chat.completions.create.call_args
        prompt = kwargs["messages"][1]["content"]
        assert "s1: S1" in prompt
        assert "a: s" in prompt  # summary is "s" from _unit helper

    def test_api_failure_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch, mocker: pytest_mock.MockerFixture
    ) -> None:
        monkeypatch.setenv("FIREWORKS_API_KEY", "fake")
        mock_client = mocker.patch("dgov.sop_bundler.OpenAI")
        mock_client.return_value.chat.completions.create.side_effect = Exception("API Down")

        with pytest.raises(RuntimeError, match="LLMSopBundler failed: API Down"):
            LLMSopBundler().pick({"a": _unit("a")}, [])


class TestBundleCaching:
    def test_bundle_reuses_mapping_on_hash_match(self, tmp_path: Path, mocker: pytest_mock.MockerFixture) -> None:
        sops_dir = tmp_path / "sops"
        _write(sops_dir / "s1.md", _sop_md("s1", "S1", "Body 1"))
        plan = _flat_plan({"a": _unit("a", "Prompt")})

        # Pre-calculated hash for s1.md
        hash_val = compute_sop_set_hash(load_sops(sops_dir))

        # Mock bundler — should NOT be called if cache hits
        bundler = mocker.Mock(spec=SopBundler)

        # 1. Cache hit
        cached_mapping = {"a": ("s1",)}
        result = bundle(
            plan, sops_dir, bundler, cached_mapping=cached_mapping, cached_hash=hash_val
        )
        assert result.sop_mapping == {"a": ("s1",)}
        assert result.plan.units["a"].prompt == "Body 1\n\nPrompt"
        bundler.pick.assert_not_called()

    def test_bundle_re_calls_on_hash_mismatch(self, tmp_path: Path, mocker: pytest_mock.MockerFixture) -> None:
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

    def test_bundle_re_calls_on_missing_unit_in_cache(self, tmp_path: Path, mocker: pytest_mock.MockerFixture) -> None:
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
        assert result.plan.units["a"].prompt == "Use ruff.\n\nWrite code."
        assert result.sop_mapping == {"a": ("style",)}

    def test_multiple_sops_concatenated(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(sops_dir / "a.md", _sop_md("alpha", "A", "Alpha body."))
        _write(sops_dir / "b.md", _sop_md("beta", "B", "Beta body."))
        plan = _flat_plan({"x": _unit("x", "Task prompt.")})
        result = bundle(plan, sops_dir, _PickAllBundler())
        assert result.plan.units["x"].prompt == "Alpha body.\n\nBeta body.\n\nTask prompt."

    def test_selective_assignment(self, tmp_path: Path) -> None:
        sops_dir = tmp_path / "sops"
        _write(sops_dir / "lint.md", _sop_md("lint", "Lint", "Run linter."))
        _write(sops_dir / "test.md", _sop_md("test", "Test", "Run tests."))
        plan = _flat_plan(
            {
                "a": _unit("a", "Task A."),
                "b": _unit("b", "Task B."),
            }
        )
        mapping = {"a": ["lint"], "b": ["test"]}
        result = bundle(plan, sops_dir, _SelectiveBundler(mapping))
        assert result.plan.units["a"].prompt == "Run linter.\n\nTask A."
        assert result.plan.units["b"].prompt == "Run tests.\n\nTask B."
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
        assert result.plan.units["a"].prompt == "Body.\n\nPrompt A."
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
        plan_root = Path(__file__).parent.parent / ".dgov" / "plans" / "plan-system"
        sops_dir = plan_root.parent.parent.parent / "sops"  # .dgov/sops
        plan = resolve_refs(merge_tree(walk_tree(plan_root)))
        result = bundle(plan, sops_dir, IdentityBundler())
        # All unit prompts unchanged (no SOPs exist yet)
        for uid in plan.units:
            assert result.plan.units[uid].prompt == plan.units[uid].prompt
        assert result.sop_set_hash == ""
