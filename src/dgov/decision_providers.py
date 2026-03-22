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
            model_id="deterministic",
            confidence=1.0,
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
            confidence=1.0,
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
            confidence=0.7,
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
            model_id="deterministic",
            confidence=1.0,
            artifact=review,
            latency_ms=latency_ms,
            trace_id=request.trace_id,
        )


@dataclass
class ModelReviewProvider(DecisionProvider):
    """Review provider that sends the diff to a specified model for quality review.

    Used as the second tier in the review cascade — only fires when the
    deterministic InspectionReviewProvider passes and a review_agent is specified.
    The model reviews logic and design quality, not syntax (code already passes tests).
    """

    provider_id: str = "model-review"

    def capabilities(self) -> frozenset[DecisionKind]:
        return frozenset({DecisionKind.REVIEW_OUTPUT})

    def review_output(self, request: ReviewOutputRequest) -> DecisionRecord[ReviewOutputDecision]:
        if not request.review_agent:
            raise ProviderError("ModelReviewProvider requires review_agent")

        from dgov.openrouter import _openrouter_request

        # Build the review context
        diff = request.diff
        if not diff and request.project_root and request.slug:
            from dgov.inspection import review_worker_pane

            review = review_worker_pane(
                request.project_root,
                request.slug,
                session_root=request.session_root,
            )
            diff = review.get("diff", review.get("stat", ""))

        if not diff:
            raise ProviderError("No diff available for model review")

        # Map logical agent name to OpenRouter model
        model = _resolve_review_model(request.review_agent)

        started = time.perf_counter()

        prompt = (
            "Review this code diff. The code already passes tests and lint.\n"
            "Focus on: logic correctness, edge cases, design quality.\n"
            "Do NOT flag style issues — only real bugs or design concerns.\n\n"
            f"## Diff\n```\n{diff[:8000]}\n```\n\n"
            "Respond in exactly this format:\n"
            "VERDICT: approved | concerns\n"
            "SUMMARY: (one line)\n"
            "ISSUES: (one per line, or 'none')"
        )

        try:
            response = _openrouter_request(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                max_tokens=500,
                temperature=0,
            )
            content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
            verdict, issues, summary = _parse_review_response(content)
        except Exception as exc:
            # Model failure is not fatal — fall through gracefully
            raise ProviderError(f"Model review failed: {exc}") from exc

        latency_ms = (time.perf_counter() - started) * 1000

        return DecisionRecord(
            kind=DecisionKind.REVIEW_OUTPUT,
            provider_id=self.provider_id,
            decision=ReviewOutputDecision(
                verdict=verdict,
                commit_count=-1,  # Not applicable for model review
                issues=issues,
                reason=summary if verdict != "approved" else None,
            ),
            model_id=model,
            confidence=0.8,
            latency_ms=latency_ms,
            trace_id=request.trace_id,
        )


def _resolve_review_model(review_agent: str) -> str:
    """Map a logical agent name to an OpenRouter model identifier."""
    _MODEL_MAP = {
        "qwen-9b": "qwen/qwen3.5-9b",
        "qwen-35b": "qwen/qwen3.5-35b",
        "qwen-122b": "qwen/qwen3.5-122b",
        "qwen-397b": "qwen/qwen3.5-397b",
    }
    return _MODEL_MAP.get(review_agent, review_agent)


def _parse_review_response(content: str) -> tuple[str, tuple[str, ...], str]:
    """Parse the model's review response into (verdict, issues, summary)."""
    verdict = "approved"
    issues: list[str] = []
    summary = ""

    for line in content.splitlines():
        line_stripped = line.strip()
        upper = line_stripped.upper()
        if upper.startswith("VERDICT:"):
            raw_verdict = line_stripped.split(":", 1)[1].strip().lower()
            if "concern" in raw_verdict or "change" in raw_verdict:
                verdict = "concerns"
            else:
                verdict = "safe"
        elif upper.startswith("SUMMARY:"):
            summary = line_stripped.split(":", 1)[1].strip()
        elif upper.startswith("ISSUES:"):
            rest = line_stripped.split(":", 1)[1].strip()
            if rest.lower() != "none" and rest:
                issues.append(rest)
        elif issues and line_stripped and not upper.startswith(("VERDICT", "SUMMARY")):
            # Continuation of issues list
            issues.append(line_stripped)

    return verdict, tuple(issues), summary


@dataclass
class StatisticalRoutingProvider(DecisionProvider):
    """Route tasks using historical success rates from the spans table.

    Reads dispatch/review/retry spans to compute per-agent pass rates.
    Picks the best-performing agent with sufficient sample size.
    Falls through to LLM routing via ProviderError when data is insufficient.
    """

    provider_id: str = "statistical-routing"
    session_root: str = ""
    min_samples: int = 5  # minimum dispatches before trusting the data

    def capabilities(self) -> frozenset[DecisionKind]:
        return frozenset({DecisionKind.ROUTE_TASK})

    def route_task(self, request: RouteTaskRequest) -> DecisionRecord[RouteTaskDecision]:
        from dgov.spans import agent_reliability_stats

        stats = agent_reliability_stats(self.session_root, min_dispatches=self.min_samples)

        if not stats:
            raise ProviderError("insufficient span data for statistical routing")

        best_agent = max(stats.keys(), key=lambda a: stats[a]["pass_rate"])
        best = stats[best_agent]
        pass_rate = best["pass_rate"]
        dispatch_count = best["dispatch_count"]

        reason = (
            f"statistical: {pass_rate:.0%} pass rate over {dispatch_count} dispatches (from spans)"
        )
        return DecisionRecord(
            kind=DecisionKind.ROUTE_TASK,
            provider_id=self.provider_id,
            decision=RouteTaskDecision(agent=best_agent, reason=reason),
            model_id="statistical",
            confidence=pass_rate,
            trace_id=request.trace_id,
        )
