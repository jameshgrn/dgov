"""Kimi K2.5 baseline agent for HAL benchmarks.

Single-call, no tool loop, no dgov. This is the floor — it shows what
the raw model produces when given only the problem statement. Compare
against dgov_pipeline to measure the value of the governor harness.

Requires:
  - FIREWORKS_API_KEY set
  - openai SDK installed
"""

from __future__ import annotations

import os

from openai import OpenAI

_SYSTEM_PROMPT = """\
You are an expert software engineer. You will be given a GitHub issue
description for an open-source Python repository. Your job is to produce
a minimal, correct git patch (unified diff format) that resolves the issue.

Output ONLY the patch. No explanation, no markdown fences.
"""


def run(input: dict[str, dict], **kwargs) -> dict:
    """HAL agent entry point — raw Kimi K2.5 single call."""
    assert len(input) == 1
    task_id, task = next(iter(input.items()))

    problem_statement = task["problem_statement"]
    model = kwargs.get(
        "model_name",
        "accounts/fireworks/models/kimi-k2p5-turbo",
    )

    client = OpenAI(
        base_url="https://api.fireworks.ai/inference/v1",
        api_key=os.environ["FIREWORKS_API_KEY"],
    )

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": problem_statement},
        ],
        max_tokens=8000,
        temperature=0.0,
    )

    content = response.choices[0].message.content or ""
    usage = response.usage
    cost = 0.0  # TODO: calculate from token counts + Fireworks pricing

    return {
        task_id: {
            "history": [{"role": "assistant", "content": content}],
            "cost": cost,
            "tokens": {
                "prompt": usage.prompt_tokens if usage else 0,
                "completion": usage.completion_tokens if usage else 0,
            },
        }
    }
