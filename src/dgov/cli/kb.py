"""Knowledge base CLI commands."""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.parse
from pathlib import Path

import click

from dgov.cli import cli, want_json
from dgov.kb import (
    KnowledgeArticle,
    KnowledgeIssue,
    article_by_id,
    build_knowledge_graph,
    collect_knowledge_base,
)
from dgov.project_root import resolve_project_root


@cli.group(name="kb")
def kb_cmd() -> None:
    """Browse and validate the repo knowledge base."""
    pass


@kb_cmd.command(name="list")
@click.option("--root", "-r", default=".", help="Project root")
def kb_list(root: str) -> None:
    """List knowledge base articles."""
    project_root = resolve_project_root(Path(root))
    articles, issues = collect_knowledge_base(project_root)
    _exit_on_issues(issues)

    if want_json():
        click.echo(
            json.dumps(
                {"articles": [_article_payload(article, project_root) for article in articles]},
                indent=2,
            )
        )
        return

    if not articles:
        click.echo("No knowledge base articles found.")
        return

    click.echo("Knowledge base articles:")
    id_width = max(len(article.id) for article in articles)
    for article in articles:
        click.echo(
            f"  {article.id:{id_width}s} {article.kind:12s} "
            f"{article.status:8s} {article.relative_path}"
        )


@kb_cmd.command(name="show")
@click.argument("article_id")
@click.option("--root", "-r", default=".", help="Project root")
def kb_show(article_id: str, root: str) -> None:
    """Show a knowledge base article by id."""
    project_root = resolve_project_root(Path(root))
    article, issues = article_by_id(project_root, article_id)
    _exit_on_issues(issues)
    if article is None:
        click.echo(f"Error: unknown article id: {article_id}", err=True)
        raise click.exceptions.Exit(code=1)

    if want_json():
        payload = _article_payload(article, project_root)
        payload["body"] = article.body
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo(f"path: {article.relative_path}")
    click.echo(f"kind: {article.kind}")
    click.echo(f"status: {article.status}")
    click.echo("sources:")
    for source in article.sources:
        click.echo(f"  - {source}")
    if article.related:
        click.echo("related:")
        for related in article.related:
            click.echo(f"  - {related}")
    click.echo()
    click.echo(article.body)


@kb_cmd.command(name="validate")
@click.option("--root", "-r", default=".", help="Project root")
def kb_validate(root: str) -> None:
    """Validate knowledge base article metadata and links."""
    project_root = resolve_project_root(Path(root))
    articles, issues = collect_knowledge_base(project_root)

    if want_json():
        click.echo(
            json.dumps(
                {
                    "status": "fail" if issues else "pass",
                    "article_count": len(articles),
                    "issues": [_issue_payload(issue) for issue in issues],
                },
                indent=2,
            )
        )
    elif issues:
        _echo_issues(issues)
    else:
        click.echo(f"Knowledge base valid: {len(articles)} article(s).")

    if issues:
        raise click.exceptions.Exit(code=1)


@kb_cmd.command(name="graph")
@click.option("--root", "-r", default=".", help="Project root")
def kb_graph(root: str) -> None:
    """Show the knowledge base graph."""
    project_root = resolve_project_root(Path(root))
    articles, issues = collect_knowledge_base(project_root)
    _exit_on_issues(issues)
    graph = build_knowledge_graph(articles)

    if want_json():
        payload = {
            "articles": sorted(graph.article_nodes),
            "sources": sorted(graph.source_nodes),
            "edges": [
                {"source": e.source, "target": e.target, "relation": e.relation}
                for e in graph.edges
            ],
        }
        click.echo(json.dumps(payload, indent=2))
        return

    if not graph.article_nodes:
        click.echo("No knowledge base articles found.")
        return

    click.echo(f"Articles ({len(graph.article_nodes)}):")
    for aid in sorted(graph.article_nodes):
        click.echo(f"  {aid}")

    if graph.source_nodes:
        click.echo(f"Sources ({len(graph.source_nodes)}):")
        for src in sorted(graph.source_nodes):
            click.echo(f"  {src}")

    if graph.edges:
        click.echo("Edges:")
        for edge in graph.edges:
            click.echo(f"  {edge.source} --[{edge.relation}]--> {edge.target}")


@kb_cmd.command(name="related")
@click.argument("article_id")
@click.option("--depth", "-d", default=1, type=int, help="Traversal depth")
@click.option("--root", "-r", default=".", help="Project root")
def kb_related(article_id: str, depth: int, root: str) -> None:
    """Show article ids reachable through related edges."""
    project_root = resolve_project_root(Path(root))
    articles, issues = collect_knowledge_base(project_root)
    _exit_on_issues(issues)
    graph = build_knowledge_graph(articles)

    if article_id not in graph.article_nodes:
        click.echo(f"Error: unknown article id: {article_id}", err=True)
        raise click.exceptions.Exit(code=1)

    related = graph.related_by_depth(article_id, depth)

    if want_json():
        click.echo(
            json.dumps({"start": article_id, "depth": depth, "related": sorted(related)}, indent=2)
        )
        return

    if not related:
        click.echo(f"No articles related to {article_id} within depth {depth}.")
        return

    click.echo(f"Articles related to {article_id} (depth {depth}):")
    for rid in sorted(related):
        click.echo(f"  {rid}")


@kb_cmd.command(name="path")
@click.argument("from_id")
@click.argument("to_id")
@click.option("--root", "-r", default=".", help="Project root")
def kb_path(from_id: str, to_id: str, root: str) -> None:
    """Show the shortest related-edge path between two articles."""
    project_root = resolve_project_root(Path(root))
    articles, issues = collect_knowledge_base(project_root)
    _exit_on_issues(issues)
    graph = build_knowledge_graph(articles)

    if from_id not in graph.article_nodes:
        click.echo(f"Error: unknown article id: {from_id}", err=True)
        raise click.exceptions.Exit(code=1)
    if to_id not in graph.article_nodes:
        click.echo(f"Error: unknown article id: {to_id}", err=True)
        raise click.exceptions.Exit(code=1)

    path = graph.shortest_related_path(from_id, to_id)

    if want_json():
        click.echo(json.dumps({"from": from_id, "to": to_id, "path": path}, indent=2))
        return

    if path is None:
        click.echo(f"No related-edge path from {from_id} to {to_id}.")
        return

    click.echo(" -> ".join(path))


@kb_cmd.command(name="open")
@click.argument("article_id")
@click.option("--root", "-r", default=".", help="Project root")
def kb_open(article_id: str, root: str) -> None:
    """Open a knowledge base article in Obsidian."""
    project_root = resolve_project_root(Path(root))
    articles, issues = collect_knowledge_base(project_root)
    _exit_on_issues(issues)

    by_id = {a.id: a for a in articles}
    article = by_id.get(article_id)
    if article is None:
        click.echo(f"Error: unknown article id: {article_id}", err=True)
        raise click.exceptions.Exit(code=1)

    absolute_path = (project_root / article.relative_path).resolve()
    if shutil.which("obsidian"):
        subprocess.run(["obsidian", "open", article.relative_path], check=False)
        return

    uri = f"obsidian://open?path={urllib.parse.quote(str(absolute_path))}"
    click.echo(uri)


def _exit_on_issues(issues: list[KnowledgeIssue]) -> None:
    if not issues:
        return
    if want_json():
        click.echo(json.dumps({"status": "fail", "issues": [_issue_payload(i) for i in issues]}))
    else:
        _echo_issues(issues)
    raise click.exceptions.Exit(code=1)


def _echo_issues(issues: list[KnowledgeIssue]) -> None:
    click.echo("Knowledge base validation failed:", err=True)
    for issue in issues:
        click.echo(f"  {issue.path}: {issue.message}", err=True)


def _article_payload(article: KnowledgeArticle, project_root: Path) -> dict[str, object]:
    return {
        "id": article.id,
        "title": article.title,
        "kind": article.kind,
        "status": article.status,
        "path": article.relative_path,
        "absolute_path": str((project_root / article.relative_path).resolve()),
        "sources": list(article.sources),
        "related": list(article.related),
    }


def _issue_payload(issue: KnowledgeIssue) -> dict[str, str]:
    return {"path": issue.path, "message": issue.message}
