"""Tests for dgov.router — logical agent name resolution."""

import json
import time

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


@pytest.mark.unit
class TestCircuitBreaker:
    def test_record_creates_file(self, tmp_path):
        from dgov.router import record_backend_failure

        record_backend_failure(str(tmp_path), "test-backend")
        failures_file = tmp_path / ".dgov" / "backend_failures.json"
        assert failures_file.exists()
        data = json.loads(failures_file.read_text())
        assert "test-backend" in data
        assert len(data["test-backend"]) == 1

    def test_record_appends(self, tmp_path):
        from dgov.router import record_backend_failure

        record_backend_failure(str(tmp_path), "test-backend")
        record_backend_failure(str(tmp_path), "test-backend")
        data = json.loads((tmp_path / ".dgov" / "backend_failures.json").read_text())
        assert len(data["test-backend"]) == 2

    def test_record_prunes_old(self, tmp_path):
        from dgov.router import record_backend_failure

        failures_file = tmp_path / ".dgov" / "backend_failures.json"
        failures_file.parent.mkdir(parents=True, exist_ok=True)
        old_ts = time.time() - 700  # > 10 minutes ago
        failures_file.write_text(json.dumps({"test-backend": [old_ts]}))
        record_backend_failure(str(tmp_path), "test-backend")
        data = json.loads(failures_file.read_text())
        assert len(data["test-backend"]) == 1  # old one pruned

    def test_check_true_when_threshold_exceeded(self, tmp_path):
        from dgov.router import _check_circuit_breaker, record_backend_failure

        record_backend_failure(str(tmp_path), "bad-backend")
        record_backend_failure(str(tmp_path), "bad-backend")
        assert _check_circuit_breaker(str(tmp_path), "bad-backend") is True

    def test_check_false_under_threshold(self, tmp_path):
        from dgov.router import _check_circuit_breaker, record_backend_failure

        record_backend_failure(str(tmp_path), "ok-backend")
        assert _check_circuit_breaker(str(tmp_path), "ok-backend") is False

    def test_check_false_missing_file(self, tmp_path):
        from dgov.router import _check_circuit_breaker

        assert _check_circuit_breaker(str(tmp_path), "any-backend") is False

    def test_resolve_skips_tripped_backend(self, tmp_path, monkeypatch):
        from dgov.router import record_backend_failure

        monkeypatch.setattr(
            "dgov.router._load_routing_tables",
            lambda: {"qwen-test": ["tripped-one", "healthy-one"]},
        )

        class FakeAgent:
            health_check = None
            max_concurrent = None
            groups = ()

        monkeypatch.setattr(
            "dgov.agents.load_registry",
            lambda *a, **kw: {"tripped-one": FakeAgent(), "healthy-one": FakeAgent()},
        )
        monkeypatch.setattr("dgov.agents.load_groups", lambda *a, **kw: {})
        monkeypatch.setattr("dgov.status._count_active_agent_workers", lambda *a: 0)

        class FakeBackend:
            def bulk_info(self):
                return {}

        monkeypatch.setattr("dgov.backend.get_backend", lambda: FakeBackend())
        monkeypatch.setattr("dgov.persistence.all_panes", lambda *a: [])

        # Trip the circuit breaker for tripped-one
        record_backend_failure(str(tmp_path), "tripped-one")
        record_backend_failure(str(tmp_path), "tripped-one")

        resolved, routed_from = resolve_agent("qwen-test", str(tmp_path), str(tmp_path))
        assert resolved == "healthy-one"
        assert routed_from == "qwen-test"
