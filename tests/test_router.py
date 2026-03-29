"""Tests for dgov.router — logical agent name resolution."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import MagicMock

import pytest

from dgov.router import (
    BackendId,
    DegradationError,
    DegradationReason,
    DegradationState,
    _check_circuit_breaker,
    _load_routing_tables,
    available_names,
    is_routable,
    record_backend_failure,
    resolve_agent,
    resolve_role,
)


@pytest.mark.unit
class TestLoadRoutingTables:
    def test_returns_dict(self):
        result = _load_routing_tables()
        assert isinstance(result, dict)

    def test_cached_on_repeat(self):
        """Test that routing tables are cached based on mtime."""
        a = _load_routing_tables()
        b = _load_routing_tables()
        # Both should return the same cached data (same dict values, may be different objects)
        assert a == b


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
class TestDegradationError:
    def test_initialization(self):
        tried = [
            ("backend-a", DegradationReason.NOT_REGISTERED),
            ("backend-b", DegradationReason.HEALTH_FAILURE),
        ]
        failures = {
            "backend-a": [DegradationReason.NOT_REGISTERED],
            "backend-b": [DegradationReason.HEALTH_FAILURE],
        }
        error = DegradationError(tried, failures)

        assert error.tried == tried
        assert error.failures == failures
        assert error.get_state() == DegradationState.FULL_FAILURE

    def test_empty_tried(self):
        tried: list[tuple[BackendId, DegradationReason]] = []
        failures: dict[BackendId, list[DegradationReason]] = {}
        error = DegradationError(tried, failures)

        assert error.tried == []
        assert error.failures == {}
        assert error.get_state() == DegradationState.NONE

    def test_get_reasons(self):
        tried = [
            ("backend-a", DegradationReason.NOT_REGISTERED),
        ]
        failures = {
            "backend-a": [DegradationReason.NOT_REGISTERED, DegradationReason.CIRCUIT_BREAKER],
        }
        error = DegradationError(tried, failures)

        reasons = error.get_reasons()
        assert DegradationReason.NOT_REGISTERED in reasons
        assert DegradationReason.CIRCUIT_BREAKER in reasons

    def test_has_full_failure(self):
        tried = [
            ("backend-a", DegradationReason.NOT_REGISTERED),
            ("backend-b", DegradationReason.HEALTH_FAILURE),
        ]
        failures = {
            "backend-a": [DegradationReason.NOT_REGISTERED],
            "backend-b": [DegradationReason.HEALTH_FAILURE],
        }
        error = DegradationError(tried, failures)

        assert error.has_full_failure() is True


@pytest.mark.unit
class TestDegradationReason:
    def test_is_str_enum(self):
        reason = DegradationReason.NOT_REGISTERED
        assert isinstance(reason, DegradationReason)
        assert isinstance(reason, str)
        assert reason.value == "not_registered"

    def test_reason_values(self):
        assert DegradationReason.NOT_REGISTERED.value == "not_registered"
        assert DegradationReason.CIRCUIT_BREAKER.value == "circuit_breaker"
        assert DegradationReason.GROUP_BLOCKED.value == "group_blocked"
        assert DegradationReason.HEALTH_FAILURE.value == "health_failure"
        assert DegradationReason.HEALTH_TIMEOUT.value == "health_timeout"
        assert DegradationReason.CONCURRENT_LIMIT.value == "concurrent_limit"


@pytest.mark.unit
class TestDegradationState:
    def test_is_str_enum(self):
        state = DegradationState.NONE
        assert isinstance(state, DegradationState)
        assert isinstance(state, str)
        assert state.value == "none"

    def test_state_values(self):
        assert DegradationState.NONE.value == "none"
        assert DegradationState.FULL_FAILURE.value == "full_failure"


@pytest.mark.unit
class TestResolveAgent:
    def test_passthrough_for_physical_agent(self, tmp_path):
        """Physical agent names passthrough to themselves."""
        resolved, routed_from = resolve_agent("river-35b", str(tmp_path), str(tmp_path))
        assert resolved == "river-35b"
        # For physical agent names (not in routing tables), logical name is the same as physical
        assert routed_from == "river-35b"

    def test_passthrough_for_unknown_name(self, tmp_path):
        """Unknown names passthrough to themselves."""
        resolved, routed_from = resolve_agent("totally-unknown", str(tmp_path), str(tmp_path))
        assert resolved == "totally-unknown"
        # Unknown names are their own logical name
        assert routed_from == "totally-unknown"

    def test_resolve_returns_physical_backend(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "dgov.router._load_routing_tables",
            lambda *_a: {"qwen-test": ["backend-a", "backend-b"]},
        )

        from dgov.agents import HealthConfig

        class FakeAgent:
            health = HealthConfig()
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
            lambda *_a: {"qwen-test": ["sick-backend", "healthy-backend"]},
        )

        from dgov.agents import HealthConfig

        class SickAgent:
            health = HealthConfig(check="false")
            max_concurrent = None

        class HealthyAgent:
            health = HealthConfig()
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
            lambda *_a: {"qwen-test": ["busy-one", "free-one"]},
        )

        from dgov.agents import HealthConfig

        class BusyAgent:
            health = HealthConfig()
            max_concurrent = 2

        class FreeAgent:
            health = HealthConfig()
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
        """When all backends fail, raises DegradationError with typed reasons."""
        monkeypatch.setattr(
            "dgov.router._load_routing_tables",
            lambda *_a: {"qwen-test": ["missing-agent"]},
        )
        monkeypatch.setattr(
            "dgov.agents.load_registry",
            lambda *a, **kw: {},
        )

        with pytest.raises(DegradationError) as exc_info:
            resolve_agent("qwen-test", str(tmp_path), str(tmp_path))

        # Verify the error has typed degradation reasons
        assert exc_info.value.tried == [("missing-agent", DegradationReason.NOT_REGISTERED)]
        assert exc_info.value.failures == {"missing-agent": [DegradationReason.NOT_REGISTERED]}
        assert exc_info.value.get_state() == DegradationState.FULL_FAILURE

    def test_degrades_to_first_backend_when_all_are_health_blocked(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "dgov.router._load_routing_tables",
            lambda *_a: {"qwen-test": ["sick-one", "sick-two"]},
        )

        from dgov.agents import HealthConfig

        class SickAgent:
            health = HealthConfig(check="false")
            max_concurrent = None
            groups = ()

        monkeypatch.setattr(
            "dgov.agents.load_registry",
            lambda *a, **kw: {"sick-one": SickAgent(), "sick-two": SickAgent()},
        )
        monkeypatch.setattr("dgov.agents.load_groups", lambda *a, **kw: {})
        monkeypatch.setattr("dgov.status._count_active_agent_workers", lambda *a: 0)

        class FakeBackend:
            def bulk_info(self):
                return {}

        monkeypatch.setattr("dgov.backend.get_backend", lambda: FakeBackend())
        monkeypatch.setattr("dgov.persistence.all_panes", lambda *a: [])

        resolved, routed_from = resolve_agent("qwen-test", str(tmp_path), str(tmp_path))
        assert resolved == "sick-one"
        assert routed_from == "qwen-test"

    def test_resolve_agent_least_loaded(self, monkeypatch):
        """Verify router picks least-loaded backend, not first-available."""
        from dgov.router import resolve_agent

        # 3 backends: b0 has 2 workers, b1 has 0, b2 has 1
        monkeypatch.setattr(
            "dgov.router._load_routing_tables", lambda *a, **kw: {"pool": ["b0", "b1", "b2"]}
        )

        from dgov.agents import HealthConfig

        class FakeAgent:
            health = HealthConfig()
            max_concurrent = None
            groups = ()

        monkeypatch.setattr(
            "dgov.agents.load_registry",
            lambda *a, **kw: {"b0": FakeAgent(), "b1": FakeAgent(), "b2": FakeAgent()},
        )
        monkeypatch.setattr("dgov.agents.load_groups", lambda *a, **kw: {})

        counts = {"b0": 2, "b1": 0, "b2": 1}
        monkeypatch.setattr(
            "dgov.status._count_active_agent_workers", lambda sr, bid: counts.get(bid, 0)
        )

        mock_backend = MagicMock()
        mock_backend.bulk_info.return_value = {}
        monkeypatch.setattr("dgov.backend.get_backend", lambda: mock_backend)
        monkeypatch.setattr("dgov.persistence.all_panes", lambda sr: [])

        backend, logical = resolve_agent("pool", "/tmp", "/tmp")
        assert backend == "b1", f"Expected least-loaded b1, got {backend}"
        assert logical == "pool"


@pytest.mark.unit
class TestCircuitBreaker:
    def test_record_creates_file(self, tmp_path):
        record_backend_failure(str(tmp_path), "test-backend")
        failures_file = tmp_path / ".dgov" / "backend_failures.json"
        assert failures_file.exists()
        data = json.loads(failures_file.read_text())
        assert "test-backend" in data
        assert len(data["test-backend"]) == 1

    def test_record_appends(self, tmp_path):
        record_backend_failure(str(tmp_path), "test-backend")
        record_backend_failure(str(tmp_path), "test-backend")
        data = json.loads((tmp_path / ".dgov" / "backend_failures.json").read_text())
        assert len(data["test-backend"]) == 2

    def test_record_prunes_old(self, tmp_path):
        record_backend_failure(str(tmp_path), "test-backend")
        failures_file = tmp_path / ".dgov" / "backend_failures.json"
        failures_file.parent.mkdir(parents=True, exist_ok=True)
        old_ts = time.time() - 700  # > 10 minutes ago
        failures_file.write_text(json.dumps({"test-backend": [old_ts]}))
        record_backend_failure(str(tmp_path), "test-backend")
        data = json.loads(failures_file.read_text())
        assert len(data["test-backend"]) == 1  # old one pruned

    def test_record_preserves_concurrent_updates(self, tmp_path):
        backend_id = "test-backend"
        rounds = 4
        writers_per_round = 8

        def record_round() -> None:
            barrier = Barrier(writers_per_round)

            def write_once() -> None:
                barrier.wait()
                record_backend_failure(str(tmp_path), backend_id)

            with ThreadPoolExecutor(max_workers=writers_per_round) as executor:
                futures = [executor.submit(write_once) for _ in range(writers_per_round)]
                for future in futures:
                    future.result()

        for _ in range(rounds):
            record_round()

        data = json.loads((tmp_path / ".dgov" / "backend_failures.json").read_text())
        assert len(data[backend_id]) == rounds * writers_per_round

    def test_check_true_when_threshold_exceeded(self, tmp_path):
        record_backend_failure(str(tmp_path), "bad-backend")
        record_backend_failure(str(tmp_path), "bad-backend")
        assert _check_circuit_breaker(str(tmp_path), "bad-backend") is True

    def test_check_false_under_threshold(self, tmp_path):
        record_backend_failure(str(tmp_path), "ok-backend")
        assert _check_circuit_breaker(str(tmp_path), "ok-backend") is False

    def test_check_false_missing_file(self, tmp_path):
        assert _check_circuit_breaker(str(tmp_path), "any-backend") is False

    def test_resolve_skips_tripped_backend(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "dgov.router._load_routing_tables",
            lambda *_a: {"qwen-test": ["tripped-one", "healthy-one"]},
        )

        from dgov.agents import HealthConfig

        class FakeAgent:
            health = HealthConfig()
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


@pytest.mark.unit
class TestResolveRole:
    def test_direct_lt_gov_name_returns_lt_gov(self):
        assert resolve_role("lt-gov") == "lt-gov"

    def test_project_local_tables_override_user_global_for_role_inference(
        self, tmp_path, monkeypatch
    ):
        def fake_load_routing_tables(project_root=None):
            if project_root:
                return {
                    "worker": ["river-9b", "kimi-k25"],
                    "lt-gov": ["claude-sonnet"],
                }
            return {
                "lt-gov": ["claude-sonnet", "kimi-k25"],
            }

        monkeypatch.setattr("dgov.router._load_routing_tables", fake_load_routing_tables)

        assert resolve_role("kimi-k25", str(tmp_path)) == "worker"
        assert resolve_role("kimi-k25") == "lt-gov"
