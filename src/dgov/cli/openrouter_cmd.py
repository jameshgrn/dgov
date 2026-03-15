"""OpenRouter integration commands."""

from __future__ import annotations

import json
import sys

import click


@click.group()
def openrouter():
    """Manage OpenRouter LLM integration."""


@openrouter.command("status")
def openrouter_status():
    """Show API key status, default model, and connectivity."""
    from dgov.openrouter import check_status

    click.echo(json.dumps(check_status(), indent=2))


@openrouter.command("models")
def openrouter_models():
    """List available free models on OpenRouter."""
    from dgov.openrouter import list_free_models

    try:
        models = list_free_models()
        click.echo(json.dumps(models, indent=2))
    except Exception as exc:
        click.echo(json.dumps({"error": str(exc)}), err=True)
        sys.exit(1)


@openrouter.command("test")
@click.option("--prompt", "-p", default="Say hello in one word.", help="Test prompt")
@click.option("--model", "-m", default=None, help="Model to use")
def openrouter_test(prompt, model):
    """Send a test prompt and show the response."""
    from dgov.openrouter import chat_completion

    try:
        result = chat_completion(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            max_tokens=50,
            temperature=0,
        )
        answer = result["choices"][0]["message"]["content"].strip()
        click.echo(json.dumps({"response": answer, "model": result.get("model", "unknown")}))
    except Exception as exc:
        click.echo(json.dumps({"error": str(exc)}), err=True)
        sys.exit(1)
