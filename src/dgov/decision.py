"""Typed decision requests, records, and provider wrappers."""

from __future__ import annotations

import threading
import time
from abc import ABC
from dataclasses import dataclass, field
from dataclasses import replace as dataclasses_replace
from enum import StrEnum
from typing import Callable, Generic, TypeVar, cast, overload

__all__ = [
    "DecisionKind",
    "RouteTaskRequest",
    "MonitorOutputRequest",
    "ReviewOutputRequest",
    "CompletionParseRequest",
    "ClarifyRequest",
    "RouteTaskDecision",
    "MonitorOutputDecision",
    "ReviewOutputDecision",
    "CompletionParseDecision",
    "ClarifyDecision",
    "DecisionRequest",
    "DecisionPayload",
    "DecisionRecord",
    "DecisionAuditEntry",
    "ShadowDecisionResult",
    "ProviderError",
    "UnsupportedDecisionError",
    "ProviderTimeoutError",
    "DecisionProvider",
    "StaticDecisionProvider",
    "_call_kind",
    "_run_with_timeout",
    "AuditProvider",
    "TimeoutProvider",
    "ShadowProvider",
    "CascadeProvider",
    "ConsensusProvider",
]


class DecisionKind(StrEnum):
    ROUTE_TASK = "route_task"
    CLASSIFY_OUTPUT = "classify_output"
    REVIEW_OUTPUT = "review_output"
    PARSE_COMPLETION = "parse_completion"
    DISAMBIGUATE = "disambiguate"


@dataclass(frozen=True)
class RouteTaskRequest:
    prompt: str
    installed_agents: tuple[str, ...] = ()
    pane_slug: str | None = None
    trace_id: str | None = None


@dataclass(frozen=True)
class MonitorOutputRequest:
    output: str
    pane_slug: str | None = None
    trace_id: str | None = None


@dataclass(frozen=True)
class ReviewOutputRequest:
    project_root: str | None = None
    slug: str | None = None
    session_root: str | None = None
    full: bool = False
    diff: str = ""
    task_prompt: str | None = None
    file_claims: tuple[str, ...] = ()
    trace_id: str | None = None
    agent_id: str | None = None
    review_agent: str = ""  # model to use for model-backed review
    tests_pass: bool = True
    lint_clean: bool = True
    post_merge_check: str = ""
    # Eval contract context from typed persistence (never reparsed from blobs)
    evals: tuple[dict, ...] = ()  # eval dicts this unit satisfies


@dataclass(frozen=True)
class CompletionParseRequest:
    raw_output: str
    pane_slug: str | None = None
    trace_id: str | None = None


@dataclass(frozen=True)
class ClarifyRequest:
    raw_input: str
    context: str | None = None
    trace_id: str | None = None


@dataclass(frozen=True)
class RouteTaskDecision:
    agent: str
    reason: str | None = None


@dataclass(frozen=True)
class MonitorOutputDecision:
    classification: str
    reason: str | None = None


@dataclass(frozen=True)
class ReviewOutputDecision:
    verdict: str
    commit_count: int = 0
    issues: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class CompletionParseDecision:
    status: str
    files_modified: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class ClarifyDecision:
    task_prompt: str | None = None
    requires_clarification: bool = False
    clarification_question: str | None = None


DecisionRequest = (
    RouteTaskRequest
    | MonitorOutputRequest
    | ReviewOutputRequest
    | CompletionParseRequest
    | ClarifyRequest
)
DecisionPayload = (
    RouteTaskDecision
    | MonitorOutputDecision
    | ReviewOutputDecision
    | CompletionParseDecision
    | ClarifyDecision
)

TDecision = TypeVar("TDecision")


@dataclass(frozen=True)
class DecisionRecord(Generic[TDecision]):
    kind: DecisionKind
    provider_id: str
    decision: TDecision
    artifact: object | None = None
    model_id: str | None = None
    confidence: float | None = None
    latency_ms: float | None = None
    cost_usd: float | None = None
    trace_id: str | None = None
    evidence_refs: tuple[str, ...] = ()
    raw_artifact_ref: str | None = None
    created_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class DecisionAuditEntry:
    request: DecisionRequest
    result: DecisionRecord[DecisionPayload] | None
    error: str | None
    provider_id: str
    duration_ms: float
    created_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class ShadowDecisionResult:
    request: DecisionRequest
    primary: DecisionRecord[DecisionPayload]
    shadow: DecisionRecord[DecisionPayload] | None
    shadow_error: str | None = None
    created_at: float = field(default_factory=time.time)


class ProviderError(RuntimeError):
    """Base error for decision provider failures."""


class UnsupportedDecisionError(ProviderError):
    """Raised when a provider does not implement a capability."""


class ProviderTimeoutError(ProviderError):
    """Raised when a provider exceeds its allowed latency budget."""


class DecisionProvider(ABC):
    """Base decision provider with typed capability methods."""

    provider_id = "decision-provider"

    def capabilities(self) -> frozenset[DecisionKind]:
        return frozenset()

    def route_task(self, request: RouteTaskRequest) -> DecisionRecord[RouteTaskDecision]:
        raise UnsupportedDecisionError(
            f"{self.provider_id} does not support {DecisionKind.ROUTE_TASK}"
        )

    def classify_output(
        self, request: MonitorOutputRequest
    ) -> DecisionRecord[MonitorOutputDecision]:
        raise UnsupportedDecisionError(
            f"{self.provider_id} does not support {DecisionKind.CLASSIFY_OUTPUT}"
        )

    def review_output(self, request: ReviewOutputRequest) -> DecisionRecord[ReviewOutputDecision]:
        raise UnsupportedDecisionError(
            f"{self.provider_id} does not support {DecisionKind.REVIEW_OUTPUT}"
        )

    def parse_completion(
        self, request: CompletionParseRequest
    ) -> DecisionRecord[CompletionParseDecision]:
        raise UnsupportedDecisionError(
            f"{self.provider_id} does not support {DecisionKind.PARSE_COMPLETION}"
        )

    def disambiguate(self, request: ClarifyRequest) -> DecisionRecord[ClarifyDecision]:
        raise UnsupportedDecisionError(
            f"{self.provider_id} does not support {DecisionKind.DISAMBIGUATE}"
        )


@dataclass
class StaticDecisionProvider(DecisionProvider):
    """Testing and bootstrap provider with preconfigured callables."""

    provider_id: str = "static"
    route_task_fn: Callable[[RouteTaskRequest], DecisionRecord[RouteTaskDecision]] | None = None
    classify_output_fn: (
        Callable[[MonitorOutputRequest], DecisionRecord[MonitorOutputDecision]] | None
    ) = None
    review_output_fn: (
        Callable[[ReviewOutputRequest], DecisionRecord[ReviewOutputDecision]] | None
    ) = None
    parse_completion_fn: (
        Callable[[CompletionParseRequest], DecisionRecord[CompletionParseDecision]] | None
    ) = None
    disambiguate_fn: Callable[[ClarifyRequest], DecisionRecord[ClarifyDecision]] | None = None

    def capabilities(self) -> frozenset[DecisionKind]:
        kinds: set[DecisionKind] = set()
        if self.route_task_fn is not None:
            kinds.add(DecisionKind.ROUTE_TASK)
        if self.classify_output_fn is not None:
            kinds.add(DecisionKind.CLASSIFY_OUTPUT)
        if self.review_output_fn is not None:
            kinds.add(DecisionKind.REVIEW_OUTPUT)
        if self.parse_completion_fn is not None:
            kinds.add(DecisionKind.PARSE_COMPLETION)
        if self.disambiguate_fn is not None:
            kinds.add(DecisionKind.DISAMBIGUATE)
        return frozenset(kinds)

    def route_task(self, request: RouteTaskRequest) -> DecisionRecord[RouteTaskDecision]:
        if self.route_task_fn is None:
            return super().route_task(request)
        return self.route_task_fn(request)

    def classify_output(
        self, request: MonitorOutputRequest
    ) -> DecisionRecord[MonitorOutputDecision]:
        if self.classify_output_fn is None:
            return super().classify_output(request)
        return self.classify_output_fn(request)

    def review_output(self, request: ReviewOutputRequest) -> DecisionRecord[ReviewOutputDecision]:
        if self.review_output_fn is None:
            return super().review_output(request)
        return self.review_output_fn(request)

    def parse_completion(
        self, request: CompletionParseRequest
    ) -> DecisionRecord[CompletionParseDecision]:
        if self.parse_completion_fn is None:
            return super().parse_completion(request)
        return self.parse_completion_fn(request)

    def disambiguate(self, request: ClarifyRequest) -> DecisionRecord[ClarifyDecision]:
        if self.disambiguate_fn is None:
            return super().disambiguate(request)
        return self.disambiguate_fn(request)


@overload
def _call_kind(
    provider: DecisionProvider, request: RouteTaskRequest
) -> DecisionRecord[RouteTaskDecision]: ...


@overload
def _call_kind(
    provider: DecisionProvider, request: MonitorOutputRequest
) -> DecisionRecord[MonitorOutputDecision]: ...


@overload
def _call_kind(
    provider: DecisionProvider, request: ReviewOutputRequest
) -> DecisionRecord[ReviewOutputDecision]: ...


@overload
def _call_kind(
    provider: DecisionProvider, request: CompletionParseRequest
) -> DecisionRecord[CompletionParseDecision]: ...


@overload
def _call_kind(
    provider: DecisionProvider, request: ClarifyRequest
) -> DecisionRecord[ClarifyDecision]: ...


def _call_kind[TDecision](
    provider: DecisionProvider,
    request: DecisionRequest,
) -> DecisionRecord[TDecision]:
    if isinstance(request, RouteTaskRequest):
        return provider.route_task(request)  # type: ignore[return-value]
    if isinstance(request, MonitorOutputRequest):
        return provider.classify_output(request)  # type: ignore[return-value]
    if isinstance(request, ReviewOutputRequest):
        return provider.review_output(request)  # type: ignore[return-value]
    if isinstance(request, CompletionParseRequest):
        return provider.parse_completion(request)  # type: ignore[return-value]
    if isinstance(request, ClarifyRequest):
        return provider.disambiguate(request)  # type: ignore[return-value]
    raise ProviderError(f"Unsupported decision request: {type(request).__name__}")


def _run_with_timeout[TDecision](
    fn: Callable[[], DecisionRecord[TDecision]],
    timeout_s: float,
) -> DecisionRecord[TDecision]:
    value: DecisionRecord[TDecision] | None = None
    error: BaseException | None = None

    def _target() -> None:
        nonlocal error, value
        try:
            value = fn()
        except BaseException as exc:  # noqa: BLE001
            error = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        raise ProviderTimeoutError(f"Decision provider timed out after {timeout_s}s")
    if error is not None:
        raise error
    if value is None:
        raise ProviderError("Decision provider failed without an exception")
    return value


@dataclass
class AuditProvider(DecisionProvider):
    """Wrapper that records every request/result boundary."""

    provider_id: str = field(init=False)
    inner: DecisionProvider
    sink: Callable[[DecisionAuditEntry], None]

    def __post_init__(self) -> None:
        self.provider_id = f"audit:{self.inner.provider_id}"

    def capabilities(self) -> frozenset[DecisionKind]:
        return self.inner.capabilities()

    def _call(self, request: DecisionRequest) -> DecisionRecord[DecisionPayload]:
        started = time.perf_counter()
        try:
            result = _call_kind(self.inner, request)
        except Exception as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            self.sink(
                DecisionAuditEntry(
                    request=request,
                    result=None,
                    error=str(exc),
                    provider_id=self.inner.provider_id,
                    duration_ms=duration_ms,
                )
            )
            raise

        duration_ms = (time.perf_counter() - started) * 1000
        self.sink(
            DecisionAuditEntry(
                request=request,
                result=result,  # type: ignore[arg-type]
                error=None,
                provider_id=self.inner.provider_id,
                duration_ms=duration_ms,
            )
        )
        return result  # type: ignore[return-value]

    def route_task(self, request: RouteTaskRequest) -> DecisionRecord[RouteTaskDecision]:
        return self._call(request)  # type: ignore[return-value]

    def classify_output(
        self, request: MonitorOutputRequest
    ) -> DecisionRecord[MonitorOutputDecision]:
        return self._call(request)  # type: ignore[return-value]

    def review_output(self, request: ReviewOutputRequest) -> DecisionRecord[ReviewOutputDecision]:
        return self._call(request)  # type: ignore[return-value]

    def parse_completion(
        self, request: CompletionParseRequest
    ) -> DecisionRecord[CompletionParseDecision]:
        return self._call(request)  # type: ignore[return-value]

    def disambiguate(self, request: ClarifyRequest) -> DecisionRecord[ClarifyDecision]:
        return self._call(request)  # type: ignore[return-value]


@dataclass
class TimeoutProvider(DecisionProvider):
    """Wrapper that bounds provider latency."""

    provider_id: str = field(init=False)
    inner: DecisionProvider
    timeout_s: float

    def __post_init__(self) -> None:
        self.provider_id = f"timeout:{self.inner.provider_id}"

    def capabilities(self) -> frozenset[DecisionKind]:
        return self.inner.capabilities()

    def _call(self, request: DecisionRequest) -> DecisionRecord[DecisionPayload]:
        return cast(
            DecisionRecord[DecisionPayload],
            _run_with_timeout(lambda: _call_kind(self.inner, request), self.timeout_s),
        )

    def route_task(self, request: RouteTaskRequest) -> DecisionRecord[RouteTaskDecision]:
        return self._call(request)  # type: ignore[return-value]

    def classify_output(
        self, request: MonitorOutputRequest
    ) -> DecisionRecord[MonitorOutputDecision]:
        return self._call(request)  # type: ignore[return-value]

    def review_output(self, request: ReviewOutputRequest) -> DecisionRecord[ReviewOutputDecision]:
        return self._call(request)  # type: ignore[return-value]

    def parse_completion(
        self, request: CompletionParseRequest
    ) -> DecisionRecord[CompletionParseDecision]:
        return self._call(request)  # type: ignore[return-value]

    def disambiguate(self, request: ClarifyRequest) -> DecisionRecord[ClarifyDecision]:
        return self._call(request)  # type: ignore[return-value]


@dataclass
class ShadowProvider(DecisionProvider):
    """Wrapper that runs a shadow provider for comparison without affecting control."""

    provider_id: str = field(init=False)
    primary: DecisionProvider
    shadow: DecisionProvider
    sink: Callable[[ShadowDecisionResult], None] | None = None

    def __post_init__(self) -> None:
        self.provider_id = f"shadow:{self.primary.provider_id}"

    def capabilities(self) -> frozenset[DecisionKind]:
        return self.primary.capabilities()

    def _call(self, request: DecisionRequest) -> DecisionRecord[DecisionPayload]:
        primary = _call_kind(self.primary, request)
        shadow_result: DecisionRecord[DecisionPayload] | None = None
        shadow_error: str | None = None
        try:
            shadow_result = _call_kind(self.shadow, request)  # type: ignore[assignment]
        except Exception as exc:
            shadow_error = str(exc)

        if self.sink is not None:
            self.sink(
                ShadowDecisionResult(
                    request=request,
                    primary=primary,  # type: ignore[arg-type]
                    shadow=shadow_result,
                    shadow_error=shadow_error,
                )
            )
        return primary  # type: ignore[return-value]

    def route_task(self, request: RouteTaskRequest) -> DecisionRecord[RouteTaskDecision]:
        return self._call(request)  # type: ignore[return-value]

    def classify_output(
        self, request: MonitorOutputRequest
    ) -> DecisionRecord[MonitorOutputDecision]:
        return self._call(request)  # type: ignore[return-value]

    def review_output(self, request: ReviewOutputRequest) -> DecisionRecord[ReviewOutputDecision]:
        return self._call(request)  # type: ignore[return-value]

    def parse_completion(
        self, request: CompletionParseRequest
    ) -> DecisionRecord[CompletionParseDecision]:
        return self._call(request)  # type: ignore[return-value]

    def disambiguate(self, request: ClarifyRequest) -> DecisionRecord[ClarifyDecision]:
        return self._call(request)  # type: ignore[return-value]


@dataclass
class CascadeProvider(DecisionProvider):
    """Wrapper that tries providers in order (cheap → expensive), falling through on failure.

    Tries each provider in sequence. If a provider succeeds and passes an optional
    validator, returns its result. If it raises ProviderError or the validator rejects
    the result, tries the next provider.

    Logs which tier actually handled the request by storing the real provider_id.
    """

    provider_id: str = field(init=False)
    inner_providers: list[DecisionProvider]
    validator: Callable[[DecisionRecord[DecisionPayload]], bool] | None = None

    def __post_init__(self) -> None:
        self.provider_id = "cascade"

    def capabilities(self) -> frozenset[DecisionKind]:
        """Union of all inner provider capabilities."""
        kinds: set[DecisionKind] = set()
        for provider in self.inner_providers:
            kinds.update(provider.capabilities())
        return frozenset(kinds)

    def _call(self, request: DecisionRequest) -> DecisionRecord[DecisionPayload]:
        last_error: BaseException | None = None

        for inner_provider in self.inner_providers:
            try:
                result = _call_kind(inner_provider, request)
            except ProviderError as exc:
                # Fall through to next provider
                last_error = exc
                continue

            # Apply optional validator
            if self.validator is not None:
                try:
                    if not self.validator(result):  # type: ignore[arg-type]
                        # Validator rejected, try next provider
                        last_error = ProviderError("Validator rejected result")
                        continue
                except Exception as exc:
                    # Validator raised exception, treat as rejection
                    last_error = exc
                    continue

            # Success - return a new record with the inner provider's ID
            return dataclasses_replace(result, provider_id=inner_provider.provider_id)  # type: ignore[return-value]

        # All providers failed
        if last_error is not None:
            raise last_error
        raise ProviderError("All cascade providers failed")

    def route_task(self, request: RouteTaskRequest) -> DecisionRecord[RouteTaskDecision]:
        return self._call(request)  # type: ignore[return-value]

    def classify_output(
        self, request: MonitorOutputRequest
    ) -> DecisionRecord[MonitorOutputDecision]:
        return self._call(request)  # type: ignore[return-value]

    def review_output(self, request: ReviewOutputRequest) -> DecisionRecord[ReviewOutputDecision]:
        return self._call(request)  # type: ignore[return-value]

    def parse_completion(
        self, request: CompletionParseRequest
    ) -> DecisionRecord[CompletionParseDecision]:
        return self._call(request)  # type: ignore[return-value]

    def disambiguate(self, request: ClarifyRequest) -> DecisionRecord[ClarifyDecision]:
        return self._call(request)  # type: ignore[return-value]


@dataclass
class ConsensusProvider(DecisionProvider):
    """Wrapper that runs two providers and escalates to tiebreaker on disagreement.

    Implements the 'disagreement as escalation signal' pattern from Policy Core:
    Never use LLM confidence scores as escalation signals. Escalation triggers:
    two cheap providers disagree (consensus), output fails property tests, or
    historical accuracy is low.
    """

    provider_id: str = field(init=False)
    provider_a: DecisionProvider
    provider_b: DecisionProvider
    tiebreaker: DecisionProvider
    agree_fn: Callable[[DecisionRecord[DecisionPayload], DecisionRecord[DecisionPayload]], bool]

    def __post_init__(self) -> None:
        self.provider_id = f"consensus:{self.provider_a.provider_id}:{self.provider_b.provider_id}"

    def capabilities(self) -> frozenset[DecisionKind]:
        """Union of all provider capabilities."""
        kinds: set[DecisionKind] = set()
        kinds.update(self.provider_a.capabilities())
        kinds.update(self.provider_b.capabilities())
        kinds.update(self.tiebreaker.capabilities())
        return frozenset(kinds)

    def _call(self, request: DecisionRequest) -> DecisionRecord[DecisionPayload]:
        # Run both cheap providers
        result_a: DecisionRecord[DecisionPayload] | None = None
        result_b: DecisionRecord[DecisionPayload] | None = None
        error_a: BaseException | None = None
        error_b: BaseException | None = None

        try:
            result_a = _call_kind(self.provider_a, request)
        except ProviderError as exc:
            error_a = exc

        try:
            result_b = _call_kind(self.provider_b, request)
        except ProviderError as exc:
            error_b = exc

        # Both failed - raise
        if result_a is None and result_b is None:
            raise ProviderError(f"Both consensus providers failed: A={error_a}, B={error_b}")

        # Only A succeeded - return it (graceful degradation)
        if result_a is not None and result_b is None:
            return result_a  # type: ignore[return-value]

        # Only B succeeded - return it (graceful degradation)
        if result_a is None and result_b is not None:
            return result_b  # type: ignore[return-value]

        # Both succeeded - check agreement
        if self.agree_fn(result_a, result_b):
            # Agree - return provider_a's result
            return result_a  # type: ignore[return-value]

        # Disagree - escalate to tiebreaker
        tiebreaker_result = _call_kind(self.tiebreaker, request)
        return tiebreaker_result  # type: ignore[return-value]

    def route_task(self, request: RouteTaskRequest) -> DecisionRecord[RouteTaskDecision]:
        return self._call(request)  # type: ignore[return-value]

    def classify_output(
        self, request: MonitorOutputRequest
    ) -> DecisionRecord[MonitorOutputDecision]:
        return self._call(request)  # type: ignore[return-value]

    def review_output(self, request: ReviewOutputRequest) -> DecisionRecord[ReviewOutputDecision]:
        return self._call(request)  # type: ignore[return-value]

    def parse_completion(
        self, request: CompletionParseRequest
    ) -> DecisionRecord[CompletionParseDecision]:
        return self._call(request)  # type: ignore[return-value]

    def disambiguate(self, request: ClarifyRequest) -> DecisionRecord[ClarifyDecision]:
        return self._call(request)  # type: ignore[return-value]
