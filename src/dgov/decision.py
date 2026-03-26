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
    "OutputClassification",
    "CompletionStatus",
    "ReviewVerdict",
    "RouteTaskRequest",
    "MonitorOutputRequest",
    "ReviewOutputRequest",
    "CompletionParseRequest",
    "ClarifyRequest",
    "GeneratePlanRequest",
    "RouteTaskDecision",
    "MonitorOutputDecision",
    "ReviewOutputDecision",
    "CompletionParseDecision",
    "ClarifyDecision",
    "GeneratePlanDecision",
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
    GENERATE_PLAN = "generate_plan"


class OutputClassification(StrEnum):
    WORKING = "working"
    COMMITTING = "committing"
    DONE = "done"
    STUCK = "stuck"
    IDLE = "idle"
    WAITING_INPUT = "waiting_input"
    UNKNOWN = "unknown"


class CompletionStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class ReviewVerdict(StrEnum):
    SAFE = "safe"
    CONCERNS = "concerns"
    APPROVED = "approved"
    UNSAFE = "unsafe"


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
    diff: str | None = None
    task_prompt: str | None = None
    file_claims: tuple[str, ...] = ()
    trace_id: str | None = None
    agent_id: str | None = None
    review_agent: str | None = None  # model to use for model-backed review
    tests_pass: bool = True
    lint_clean: bool = True
    post_merge_check: str | None = None
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
class GeneratePlanRequest:
    """Request to generate a plan TOML from goal and file context."""

    goal: str
    files: tuple[str, ...] = ()
    file_contents: tuple[tuple[str, str], ...] = ()  # (path, content) pairs
    plan_examples: tuple[str, ...] = ()  # past plan TOML strings for few-shot
    constraints: tuple[str, ...] = ()  # e.g. "single unit only", "no new files"
    active_claims: tuple[str, ...] = ()  # files claimed by active DAGs
    trace_id: str | None = None


@dataclass(frozen=True)
class RouteTaskDecision:
    agent: str
    reason: str | None = None


@dataclass(frozen=True)
class MonitorOutputDecision:
    classification: OutputClassification
    reason: str | None = None


@dataclass(frozen=True)
class ReviewOutputDecision:
    verdict: ReviewVerdict
    commit_count: int = 0
    issues: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class CompletionParseDecision:
    status: CompletionStatus
    files_modified: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class ClarifyDecision:
    task_prompt: str | None = None
    requires_clarification: bool = False
    clarification_question: str | None = None


@dataclass(frozen=True)
class GeneratePlanDecision:
    """Generated plan TOML with validation status."""

    plan_toml: str
    valid: bool = False
    validation_issues: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()  # planner needs clarification
    reason: str | None = None


DecisionRequest = (
    RouteTaskRequest
    | MonitorOutputRequest
    | ReviewOutputRequest
    | CompletionParseRequest
    | ClarifyRequest
    | GeneratePlanRequest
)
DecisionPayload = (
    RouteTaskDecision
    | MonitorOutputDecision
    | ReviewOutputDecision
    | CompletionParseDecision
    | ClarifyDecision
    | GeneratePlanDecision
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

    def generate_plan(self, request: GeneratePlanRequest) -> DecisionRecord[GeneratePlanDecision]:
        raise UnsupportedDecisionError(
            f"{self.provider_id} does not support {DecisionKind.GENERATE_PLAN}"
        )


_FN_TO_KIND: dict[str, DecisionKind] = {
    "route_task_fn": DecisionKind.ROUTE_TASK,
    "classify_output_fn": DecisionKind.CLASSIFY_OUTPUT,
    "review_output_fn": DecisionKind.REVIEW_OUTPUT,
    "parse_completion_fn": DecisionKind.PARSE_COMPLETION,
    "disambiguate_fn": DecisionKind.DISAMBIGUATE,
    "generate_plan_fn": DecisionKind.GENERATE_PLAN,
}

_REQUEST_DISPATCH: dict[type, str] = {
    RouteTaskRequest: "route_task",
    MonitorOutputRequest: "classify_output",
    ReviewOutputRequest: "review_output",
    CompletionParseRequest: "parse_completion",
    ClarifyRequest: "disambiguate",
    GeneratePlanRequest: "generate_plan",
}

_METHOD_TO_FN: dict[str, str] = {
    "route_task": "route_task_fn",
    "classify_output": "classify_output_fn",
    "review_output": "review_output_fn",
    "parse_completion": "parse_completion_fn",
    "disambiguate": "disambiguate_fn",
    "generate_plan": "generate_plan_fn",
}


class _DelegatingProvider(DecisionProvider):
    """Base for wrappers that route all 6 methods through _call()."""

    def _call(self, request: DecisionRequest) -> DecisionRecord[DecisionPayload]:
        raise NotImplementedError

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

    def generate_plan(self, request: GeneratePlanRequest) -> DecisionRecord[GeneratePlanDecision]:
        return self._call(request)  # type: ignore[return-value]


@dataclass
class StaticDecisionProvider(_DelegatingProvider):
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
    generate_plan_fn: (
        Callable[[GeneratePlanRequest], DecisionRecord[GeneratePlanDecision]] | None
    ) = None

    def capabilities(self) -> frozenset[DecisionKind]:
        return frozenset(
            kind for attr, kind in _FN_TO_KIND.items() if getattr(self, attr) is not None
        )

    def _call(self, request: DecisionRequest) -> DecisionRecord[DecisionPayload]:
        method_name = _REQUEST_DISPATCH.get(type(request))
        if method_name is None:
            raise UnsupportedDecisionError(f"Unsupported request: {type(request).__name__}")
        fn_attr = _METHOD_TO_FN.get(method_name)
        fn = getattr(self, fn_attr, None) if fn_attr else None
        if fn is None:
            raise UnsupportedDecisionError(f"{self.provider_id} does not support {method_name}")
        return fn(request)


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


@overload
def _call_kind(
    provider: DecisionProvider, request: GeneratePlanRequest
) -> DecisionRecord[GeneratePlanDecision]: ...


def _call_kind[TDecision](
    provider: DecisionProvider,
    request: DecisionRequest,
) -> DecisionRecord[TDecision]:
    method = _REQUEST_DISPATCH.get(type(request))
    if method is None:
        raise ProviderError(f"Unsupported decision request: {type(request).__name__}")
    return cast(DecisionRecord[TDecision], getattr(provider, method)(request))


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
class AuditProvider(_DelegatingProvider):
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


@dataclass
class TimeoutProvider(_DelegatingProvider):
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


@dataclass
class ShadowProvider(_DelegatingProvider):
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


@dataclass
class CascadeProvider(_DelegatingProvider):
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
                last_error = exc
                continue

            if self.validator is not None:
                try:
                    if not self.validator(result):  # type: ignore[arg-type]
                        last_error = ProviderError("Validator rejected result")
                        continue
                except Exception as exc:
                    last_error = exc
                    continue

            return dataclasses_replace(result, provider_id=inner_provider.provider_id)  # type: ignore[return-value]

        if last_error is not None:
            raise last_error
        raise ProviderError("All cascade providers failed")


@dataclass
class ConsensusProvider(_DelegatingProvider):
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
        def _call_provider(provider: DecisionProvider) -> DecisionRecord[DecisionPayload]:
            return cast(DecisionRecord[DecisionPayload], _call_kind(provider, request))

        result_a: DecisionRecord[DecisionPayload] | None = None
        result_b: DecisionRecord[DecisionPayload] | None = None
        error_a: BaseException | None = None
        error_b: BaseException | None = None

        try:
            result_a = _call_provider(self.provider_a)
        except ProviderError as exc:
            error_a = exc

        try:
            result_b = _call_provider(self.provider_b)
        except ProviderError as exc:
            error_b = exc

        if result_a is None and result_b is None:
            raise ProviderError(f"Both consensus providers failed: A={error_a}, B={error_b}")

        if result_a is not None and result_b is None:
            return result_a

        if result_a is None and result_b is not None:
            return result_b

        if result_a is None or result_b is None:
            raise ProviderError("Consensus provider reached an impossible state")

        if self.agree_fn(result_a, result_b):
            return result_a

        return _call_provider(self.tiebreaker)
