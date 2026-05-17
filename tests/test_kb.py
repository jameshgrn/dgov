"""Tests for knowledge base loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from dgov.kb import article_by_id, collect_knowledge_base

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_source(root: Path) -> None:
    _write(root / ".dgov" / "governor.md", "governor\n")


def _article(article_id: str = "sentrux", *, source: str = ".dgov/governor.md") -> str:
    title = article_id.replace("-", " ").title()
    return f"""---
id: {article_id}
title: {title}
kind: concept
status: living
sources:
  - {source}
related: []
---

# {title}

Source-backed article body.
"""


def test_repo_knowledge_base_is_valid() -> None:
    articles, issues = collect_knowledge_base(ROOT)

    assert issues == []
    assert {article.id for article in articles} >= {
        "index",
        "knowledge-pull-architecture",
        "sentrux",
        "settlement-flow",
        "failure-shapes",
        "ledger",
    }


def test_article_by_id_returns_article(tmp_path: Path) -> None:
    _write_source(tmp_path)
    _write(tmp_path / "docs" / "knowledge" / "concepts" / "sentrux.md", _article())

    article, issues = article_by_id(tmp_path, "sentrux")

    assert issues == []
    assert article is not None
    assert article.title == "Sentrux"


def test_collect_flags_missing_source_and_unknown_related(tmp_path: Path) -> None:
    _write_source(tmp_path)
    _write(
        tmp_path / "docs" / "knowledge" / "concepts" / "sentrux.md",
        """---
id: sentrux
title: Sentrux
kind: concept
status: living
sources:
  - missing.md
related:
  - settlement-flow
---

# Sentrux

Source-backed article body.
""",
    )

    _articles, issues = collect_knowledge_base(tmp_path)

    assert ("docs/knowledge/concepts/sentrux.md", "source does not exist: missing.md") in {
        (issue.path, issue.message) for issue in issues
    }
    assert ("docs/knowledge/concepts/sentrux.md", "unknown related id: settlement-flow") in {
        (issue.path, issue.message) for issue in issues
    }


def test_collect_rejects_kb_source(tmp_path: Path) -> None:
    _write_source(tmp_path)
    _write(tmp_path / "docs" / "knowledge" / "_index.md", _article("index"))
    _write(
        tmp_path / "docs" / "knowledge" / "concepts" / "sentrux.md",
        _article(source="docs/knowledge/_index.md"),
    )

    _articles, issues = collect_knowledge_base(tmp_path)

    assert (
        "docs/knowledge/concepts/sentrux.md",
        "source must point to canonical repo state, not the KB: docs/knowledge/_index.md",
    ) in {(issue.path, issue.message) for issue in issues}


def test_collect_rejects_directory_source(tmp_path: Path) -> None:
    _write_source(tmp_path)
    (tmp_path / "src").mkdir()
    _write(
        tmp_path / "docs" / "knowledge" / "concepts" / "sentrux.md",
        _article(source="src"),
    )

    _articles, issues = collect_knowledge_base(tmp_path)

    assert ("docs/knowledge/concepts/sentrux.md", "source is not a file: src") in {
        (issue.path, issue.message) for issue in issues
    }


def test_collect_requires_first_h1_to_match_title(tmp_path: Path) -> None:
    _write_source(tmp_path)
    _write(
        tmp_path / "docs" / "knowledge" / "concepts" / "sentrux.md",
        _article().replace("# Sentrux", "# Wrong"),
    )

    _articles, issues = collect_knowledge_base(tmp_path)

    assert ("docs/knowledge/concepts/sentrux.md", "first H1 must match title") in {
        (issue.path, issue.message) for issue in issues
    }
