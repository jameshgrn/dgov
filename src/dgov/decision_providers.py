"""Concrete decision providers built on existing dgov transports."""

from __future__ import annotations

import time
from dataclasses import dataclass

from dgov.decision import (
    DecisionKind,
    DecisionProvider,
    DecisionRecord,
    MonitorOutputDecision,
    MonitorOutputRequest,
    ProviderError,
    ReviewOutputDecision,
    ReviewOutputRequest,
    RouteTaskDecision,
    RouteTaskRequest,
)


@dataclass
class DeterministicClassificationProvider(DecisionProvider):
    """Deterministic classification provider backed by regex patterns.

    Wraps the _classify_deterministic() function from monitor.py to classify
    worker output using regex patterns before falling through to LLM-based
    classification. Returns ProviderError if input is ambiguous (no pattern matched).
    """

    provider_id: str = "deterministic-classifier"

    def capabilities(self) -> frozenset[DecisionKind]:
        return frozenset({DecisionKind.CLASSIFY_OUTPUT})

    def classify_output(
        self, request: MonitorOutputRequest
    ) -> DecisionRecord[MonitorOutputDecision]:
        from dgov.monitor import _classify_deterministic

        classification = _classify_deterministic(request.output)

        if classification is None:
            # No pattern matched - ambiguous, fall through to next provider
            raise ProviderError("No deterministic pattern matched")

        return DecisionRecord(
            kind=DecisionKind.CLASSIFY_OUTPUT,
            provider_id=self.provider_id,
            decision=MonitorOutputDecision(classification=classification),
            trace_id=request.trace_id,
        )


@dataclass
class OpenRouterRoutingProvider(DecisionProvider):
    """Route-task provider backed by the existing OpenRouter classification path."""

    provider_id: str = "openrouter-routing"

    def capabilities(self) -> frozenset[DecisionKind]:
        return frozenset({DecisionKind.ROUTE_TASK})

    def route_task(self, request: RouteTaskRequest) -> DecisionRecord[RouteTaskDecision]:
        from dgov.openrouter import chat_completion

        started = time.perf_counter()
        agents = list(request.installed_agents) or ["pi", "claude"]
        use_multi = len(agents) > 2

        if use_multi:
            agent_list = ", ".join(f"'{a}'" for a in agents)
            system_msg = (
                f"Classify this task to one of these agents: {agent_list}.\n"
                "pi = mechanical: run a command, edit a specific line, "
                "add a comment, format files, simple find-and-replace.\n"
                "claude = analytical: debug why something fails, read and "
                "understand complex code, refactor architecture, fix flaky "
                "tests, multi-file reasoning, rework/redesign a system.\n"
                "codex = batch code changes, large-scale refactors, "
                "tasks that benefit from parallel execution.\n"
                "gemini = research, summarization, documentation tasks.\n"
                f"Reply with ONLY one of: {agent_list}. Nothing else."
            )
        else:
            system_msg = (
                "Classify this task as either 'pi' or 'claude'.\n"
                "pi = mechanical: run a command, edit a specific line, "
                "add a comment, format files, simple find-and-replace.\n"
                "claude = analytical: debug why something fails, read and "
                "understand complex code, refactor architecture, fix flaky "
                "tests, multi-file reasoning, rework/redesign a system, "
                "add a new feature with multiple moving parts, "
                "anything involving scheduler.py or panes.py.\n"
                "Reply with ONLY 'pi' or 'claude', nothing else."
            )

        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": request.prompt[:300]},
        ]

        try:
            response = chat_completion(messages, max_tokens=5, temperature=0)
        except RuntimeError as exc:
            raise ProviderError(str(exc)) from exc

        choices = response.get("choices") or []
        if not choices:
            raise ProviderError("Routing provider returned no choices")

        content = choices[0].get("message", {}).get("content") or ""
        answer = content.strip().lower()
        selected = "claude"
        for agent in agents:
            if agent in answer:
                selected = agent
                break

        return DecisionRecord(
            kind=DecisionKind.ROUTE_TASK,
            provider_id=self.provider_id,
            model_id=response.get("model"),
            decision=RouteTaskDecision(agent=selected),
            latency_ms=(time.perf_counter() - started) * 1000,
            trace_id=request.trace_id,
        )


@dataclass
class LocalOutputClassificationProvider(DecisionProvider):
    """Classify ambiguous worker output using the local-first monitor path."""

    provider_id: str = "local-output-classifier"

    def capabilities(self) -> frozenset[DecisionKind]:
        return frozenset({DecisionKind.CLASSIFY_OUTPUT})

    def classify_output(
        self, request: MonitorOutputRequest
    ) -> DecisionRecord[MonitorOutputDecision]:
        from dgov.openrouter import chat_completion_local_first

        started = time.perf_counter()
        messages = [
            {
                "role": "system",
                "content": (
                    "Classify the coding agent output into exactly one category. "
                    "Reply with ONE word only: "
                    "working, done, stuck, idle, waiting_input, or committing.\n"
                    "\n"
                    "Categories:\n"
                    "- working: actively writing code, running commands, exploring\n"
                    "- done: task complete, ready to commit, successful finish message\n"
                    "- stuck: error messages, exceptions, repeated failed attempts, frozen state\n"
                    "- idle: no activity, paused without work, silent for extended period\n"
                    "- waiting_input: explicitly waiting for user confirmation/input/feedback\n"
                    "- committing: running git commands, preparing to push changes\n"
                    "\n"
                    "Few-shot examples:\n"
                    "\n"
                    "Example 1:\n"
                    'Output: "Let me create the database schema first."\n'
                    "Classification: working\n"
                    "\n"
                    "Example 2:\n"
                    'Output: "I\'ve finished implementing the feature. All tests pass."\n'
                    "Classification: done\n"
                    "\n"
                    "Example 3:\n"
                    'Output: "Connection failed again after 3 attempts. Error: '
                    'ConnectionRefusedError"\n'
                    "Classification: stuck\n"
                    "\n"
                    "Example 4:\n"
                    'Output: "No active work detected in last 60 seconds"\n'
                    "Classification: idle\n"
                    "\n"
                    "Example 5:\n"
                    'Output: "Waiting for your confirmation before proceeding with the '
                    'refactoring."\n'
                    "Classification: waiting_input\n"
                    "\n"
                    "Example 6:\n"
                    "Output: \"git add src/ && git commit -m 'Add new feature'\"\n"
                    "Classification: committing\n"
                    "\n"
                    "Respond with ONLY the category name, nothing else."
                ),
            },
            {"role": "user", "content": request.output[-2000:]},
        ]

        try:
            response = chat_completion_local_first(messages, max_tokens=10, temperature=0)
        except RuntimeError as exc:
            raise ProviderError(str(exc)) from exc

        choices = response.get("choices") or []
        if not choices:
            raise ProviderError("Output classifier returned no choices")

        content = choices[0].get("message", {}).get("content") or ""
        classification = content.strip().lower()
        if classification not in {
            "working",
            "done",
            "stuck",
            "idle",
            "waiting_input",
            "committing",
        }:
            classification = "unknown"

        return DecisionRecord(
            kind=DecisionKind.CLASSIFY_OUTPUT,
            provider_id=self.provider_id,
            model_id=response.get("model"),
            decision=MonitorOutputDecision(classification=classification),
            latency_ms=(time.perf_counter() - started) * 1000,
            trace_id=request.trace_id,
        )


@dataclass
class InspectionReviewProvider(DecisionProvider):
    """Review provider backed by the existing pane inspection transport."""

    provider_id: str = "inspection-review"

    def capabilities(self) -> frozenset[DecisionKind]:
        return frozenset({DecisionKind.REVIEW_OUTPUT})

    def review_output(self, request: ReviewOutputRequest) -> DecisionRecord[ReviewOutputDecision]:
        from dgov.inspection import review_worker_pane

        if not request.project_root or not request.slug:
            raise ProviderError("Review output requests require project_root and slug")

        started = time.perf_counter()
        review = review_worker_pane(
            request.project_root,
            request.slug,
            session_root=request.session_root,
            full=request.full,
        )
        latency_ms = (time.perf_counter() - started) * 1000

        verdict = str(review.get("verdict", "unknown"))
        commit_count = int(review.get("commit_count", 0) or 0)
        issues = tuple(str(issue) for issue in review.get("issues", []) or [])
        reason = str(review.get("error")) if review.get("error") else None

        return DecisionRecord(
            kind=DecisionKind.REVIEW_OUTPUT,
            provider_id=self.provider_id,
            decision=ReviewOutputDecision(
                verdict=verdict,
                commit_count=commit_count,
                issues=issues,
                reason=reason,
            ),
            artifact=review,
            latency_ms=latency_ms,
            trace_id=request.trace_id,
        )
