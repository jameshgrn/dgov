# Eval-First Planning SOP

This is the governor procedure for writing plans that compile cleanly into dgov
execution. The core rule is simple: write the evidence of success before you
write the decomposition of work.

## Why

Agent failures are usually ambiguity failures, not syntax failures. Evals reduce
ambiguity by turning a vague task into observable conditions:

- What must become true.
- What must stay true.
- How each claim will be verified.

That makes planning more deterministic and review more mechanical.

## Planning sequence

1. Restate the user goal in one sentence.
2. Write 3-7 falsifiable evals.
3. Add 1-3 invariants or non-goals that must not regress.
4. Name the evidence for each eval.
5. Derive units only after the eval set is complete.
6. Assign exact file claims and `satisfies` links for every unit.
7. Validate the plan before running it.

## Eval checklist

Each eval should be:

- Falsifiable: a reviewer can say pass or fail.
- Observable: there is a command, output, state check, or manual check.
- Hard to game: avoid weak proxies like "a file exists" unless paired with stronger evidence.
- Relevant: it should matter to the user-facing outcome or invariant.

Use these `kind` values deliberately:

- `regression`: protects against a known bug coming back.
- `happy_path`: defines the nominal successful behavior.
- `edge`: captures boundary conditions and weird inputs.
- `invariant`: names behavior that must remain unchanged.
- `non_goal`: states what the task must not expand into.
- `manual`: reserves a check that cannot yet be made deterministic.
- `performance`: asserts latency, throughput, or resource usage bounds.
- `integration_test`: verifies cross-component integration behavior.
- `security`: asserts security properties or access control invariants.
- `scalability`: asserts the system handles growth in data, users, or load.
- `usability`: asserts user-facing workflow clarity and discoverability.
- `accessibility`: asserts inclusive design and assistive technology support.
- `reliability`: asserts fault tolerance, retry behavior, or degradation handling.
- `maintainability`: asserts code remains understandable and changeable over time.
- `testability`: asserts code is structured for easy unit and integration testing.

## Unit checklist

A plan unit is valid only if it:

- Satisfies at least one eval.
- Has exact file claims.
- Has a prompt that tells the worker what to read, what to change, and what validation to run.
- Uses `depends_on` only for real execution dependencies.

If you cannot explain which eval a unit satisfies, the unit is probably not real work.

## Anti-patterns

Avoid these:

- Writing units first and inventing evals later.
- Using vague evals like "code is cleaner" or "architecture is better."
- Using only positive cases and forgetting invariants.
- Treating `tests_pass` and `lint_clean` as the whole spec.
- Letting the same unchecked model both rewrite the spec and judge whether it succeeded.

## Minimal workflow

```bash
uv run dgov plan scratch review-refactor
$EDITOR .dgov/plans/review-refactor.toml
uv run dgov plan validate .dgov/plans/review-refactor.toml
uv run dgov plan compile .dgov/plans/review-refactor.toml
```

## Practical rule

Human supplies intent and constraints. The model may translate that into a spec
it can execute cleanly, but the eval set remains the external contract. If the
spec drifts from the evals, the evals win.
