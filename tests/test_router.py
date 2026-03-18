"""Tests for dgov.router — logical agent name resolution."""

import pytest

from dgov.router import _load_routing_tables, available_names, is_routable, resolve_agent


@pytest.mark.unit
class TestLoadRoutingTables:
    def test_returns_dict(self):
        result = _load_routing_tables()
        assert isinstance(result, dict)

    def test_cached_on_repeat(self):
        a = _load_routing_tables()
        b = _load_routing_tables()
        assert a is b


@pytest.mark.unit
class TestIsRoutable:
    def test_unknown_name(self):
        assert not is_routable("nonexistent-model-xyz")

    def test_physical_agent_not_routable(self):
        assert not is_routable("river-35b")


@pytest.mark.unit
class TestAvailableNames:
    def test_returns_sorted_list(self):
        names = available_names()
        assert isinstance(names, list)
        assert names == sorted(names)


@pytest.mark.unit
class TestResolveAgent:
    def test_passthrough_for_physical_agent(self, tmp_path):
        resolved, routed_from = resolve_agent("river-35b", str(tmp_path), str(tmp_path))
        assert resolved == "river-35b"
        assert routed_from is None

    def test_passthrough_for_unknown_name(self, tmp_path):
        resolved, routed_from = resolve_agent("totally-unknown", str(tmp_path), str(tmp_path))
        assert resolved == "totally-unknown"
        assert routed_from is None

    def test_resolve_returns_physical_backend(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "dgov.router._load_routing_tables",
            lambda: {"qwen-test": ["backend-a", "backend-b"]},
        )

        class FakeAgent:
            health_check = None
            max_concurrent = None

        monkeypatch.setattr(
            "dgov.agents.load_registry",
            lambda *a, **kw: {"backend-a": FakeAgent(), "backend-b": FakeAgent()},
        )
        monkeypatch.setattr(
            "dgov.status._count_active_agent_workers",
            lambda *a: 0,
        )

        resolved, routed_from = resolve_agent("qwen-test", str(tmp_path), str(tmp_path))
        assert resolved == "backend-a"
        assert routed_from == "qwen-test"

    def test_skips_unhealthy_backend(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "dgov.router._load_routing_tables",
            lambda: {"qwen-test": ["sick-backend", "healthy-backend"]},
        )

        class SickAgent:
            health_check = "false"
            max_concurrent = None

        class HealthyAgent:
            health_check = None
            max_concurrent = None

        monkeypatch.setattr(
            "dgov.agents.load_registry",
            lambda *a, **kw: {
                "sick-backend": SickAgent(),
                "healthy-backend": HealthyAgent(),
            },
        )
        monkeypatch.setattr(
            "dgov.status._count_active_agent_workers",
            lambda *a: 0,
        )

        resolved, routed_from = resolve_agent("qwen-test", str(tmp_path), str(tmp_path))
        assert resolved == "healthy-backend"
        assert routed_from == "qwen-test"

    def test_skips_busy_backend(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "dgov.router._load_routing_tables",
            lambda: {"qwen-test": ["busy-one", "free-one"]},
        )

        class BusyAgent:
            health_check = None
            max_concurrent = 2

        class FreeAgent:
            health_check = None
            max_concurrent = 5

        monkeypatch.setattr(
            "dgov.agents.load_registry",
            lambda *a, **kw: {
                "busy-one": BusyAgent(),
                "free-one": FreeAgent(),
            },
        )

        def fake_count(session_root, agent_id):
            return 2 if agent_id == "busy-one" else 0

        monkeypatch.setattr(
            "dgov.status._count_active_agent_workers",
            fake_count,
        )

        resolved, routed_from = resolve_agent("qwen-test", str(tmp_path), str(tmp_path))
        assert resolved == "free-one"

    def test_raises_when_all_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "dgov.router._load_routing_tables",
            lambda: {"qwen-test": ["missing-agent"]},
        )
        monkeypatch.setattr(
            "dgov.agents.load_registry",
            lambda *a, **kw: {},
        )

        with pytest.raises(RuntimeError, match="No available backend"):
            resolve_agent("qwen-test", str(tmp_path), str(tmp_path))
