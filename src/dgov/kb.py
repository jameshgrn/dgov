"""Knowledge base article loading and validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

KNOWLEDGE_DIR = Path("docs/knowledge")
VALID_KINDS = frozenset({"architecture", "concept", "index", "operation"})
VALID_STATUSES = frozenset({"draft", "living", "stable"})
_REQUIRED_FIELDS = ("id", "title", "kind", "status", "sources", "related")
_KNOWN_FIELDS = frozenset(_REQUIRED_FIELDS)
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True)
class KnowledgeArticle:
    id: str
    title: str
    kind: str
    status: str
    sources: tuple[str, ...]
    related: tuple[str, ...]
    path: Path
    relative_path: str
    body: str


@dataclass(frozen=True)
class KnowledgeIssue:
    path: str
    message: str


class KnowledgeFormatError(ValueError):
    pass


def collect_knowledge_base(
    project_root: str | Path,
) -> tuple[list[KnowledgeArticle], list[KnowledgeIssue]]:
    root = Path(project_root)
    kb_dir = root / KNOWLEDGE_DIR
    if not kb_dir.is_dir():
        return [], [KnowledgeIssue(str(KNOWLEDGE_DIR), "knowledge base directory is missing")]

    articles: list[KnowledgeArticle] = []
    issues: list[KnowledgeIssue] = []
    for path in _article_paths(kb_dir):
        relative_path = _repo_relative(root, path)
        try:
            articles.append(_parse_article(path, root, kb_dir))
        except KnowledgeFormatError as exc:
            issues.append(KnowledgeIssue(relative_path, str(exc)))

    issues.extend(_validate_articles(root, articles))
    return articles, issues


def article_by_id(
    project_root: str | Path, article_id: str
) -> tuple[KnowledgeArticle | None, list[KnowledgeIssue]]:
    articles, issues = collect_knowledge_base(project_root)
    if issues:
        return None, issues
    for article in articles:
        if article.id == article_id:
            return article, []
    return None, [KnowledgeIssue(str(KNOWLEDGE_DIR), f"unknown article id: {article_id}")]


def _article_paths(kb_dir: Path) -> list[Path]:
    return sorted(
        path for path in kb_dir.glob("**/*.md") if path.is_file() and path.name != "README.md"
    )


def _parse_article(path: Path, project_root: Path, kb_dir: Path) -> KnowledgeArticle:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise KnowledgeFormatError(f"unable to read article: {exc}") from exc

    front_matter, body = _split_front_matter(text)
    fields = _parse_front_matter(front_matter)
    _require_known_fields(fields)

    return KnowledgeArticle(
        id=_string_field(fields, "id"),
        title=_string_field(fields, "title"),
        kind=_string_field(fields, "kind"),
        status=_string_field(fields, "status"),
        sources=tuple(_list_field(fields, "sources")),
        related=tuple(_list_field(fields, "related")),
        path=path,
        relative_path=_repo_relative(project_root, path),
        body=body.strip(),
    )


def _split_front_matter(text: str) -> tuple[list[str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise KnowledgeFormatError("missing frontmatter block")
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return lines[1:index], "\n".join(lines[index + 1 :])
    raise KnowledgeFormatError("unterminated frontmatter block")


def _parse_front_matter(lines: list[str]) -> dict[str, str | list[str]]:
    fields: dict[str, str | list[str]] = {}
    current_list_key: str | None = None

    for line in lines:
        current_list_key = _parse_front_matter_line(fields, current_list_key, line)

    return fields


def _parse_front_matter_line(
    fields: dict[str, str | list[str]],
    current_list_key: str | None,
    line: str,
) -> str | None:
    if not line.strip():
        return current_list_key
    if line.startswith("  - "):
        _append_front_matter_list_item(fields, current_list_key, line)
        return current_list_key
    return _set_front_matter_field(fields, line)


def _append_front_matter_list_item(
    fields: dict[str, str | list[str]], current_list_key: str | None, line: str
) -> None:
    if current_list_key is None:
        raise KnowledgeFormatError(f"list item without list field: {line!r}")
    value = line[4:].strip().strip("\"'")
    if not value:
        raise KnowledgeFormatError(f"empty list item for field: {current_list_key}")
    list_value = fields[current_list_key]
    if not isinstance(list_value, list):
        raise KnowledgeFormatError(f"field is not a list: {current_list_key}")
    list_value.append(value)


def _set_front_matter_field(fields: dict[str, str | list[str]], line: str) -> str | None:
    if ":" not in line:
        raise KnowledgeFormatError(f"invalid frontmatter line: {line!r}")
    key, raw_value = line.split(":", 1)
    key = key.strip()
    value = raw_value.strip()
    if not key:
        raise KnowledgeFormatError(f"empty frontmatter key: {line!r}")
    if key in fields:
        raise KnowledgeFormatError(f"duplicate frontmatter field: {key}")
    if not value:
        fields[key] = []
        return key
    fields[key] = _front_matter_value(value, key)
    return None


def _front_matter_value(value: str, key: str) -> str | list[str]:
    if value.startswith("["):
        return _inline_list(value, key)
    return value.strip("\"'")


def _inline_list(value: str, key: str) -> list[str]:
    if not value.endswith("]"):
        raise KnowledgeFormatError(f"invalid inline list for field: {key}")
    inner = value[1:-1].strip()
    if not inner:
        return []
    items: list[str] = []
    for raw_item in inner.split(","):
        item = raw_item.strip().strip("\"'")
        if not item:
            raise KnowledgeFormatError(f"empty inline list item for field: {key}")
        items.append(item)
    return items


def _require_known_fields(fields: dict[str, str | list[str]]) -> None:
    missing = [field for field in _REQUIRED_FIELDS if field not in fields]
    if missing:
        raise KnowledgeFormatError(f"missing required frontmatter field(s): {', '.join(missing)}")
    unknown = sorted(set(fields) - _KNOWN_FIELDS)
    if unknown:
        raise KnowledgeFormatError(f"unknown frontmatter field(s): {', '.join(unknown)}")


def _string_field(fields: dict[str, str | list[str]], key: str) -> str:
    value = fields[key]
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeFormatError(f"frontmatter field must be a non-empty string: {key}")
    return value.strip()


def _list_field(fields: dict[str, str | list[str]], key: str) -> list[str]:
    value = fields[key]
    if not isinstance(value, list):
        raise KnowledgeFormatError(f"frontmatter field must be a list: {key}")
    return [item.strip() for item in value if item.strip()]


def _validate_articles(
    project_root: Path, articles: list[KnowledgeArticle]
) -> list[KnowledgeIssue]:
    issues: list[KnowledgeIssue] = []
    by_id: dict[str, KnowledgeArticle] = {}

    for article in articles:
        if article.id in by_id:
            issues.append(
                KnowledgeIssue(article.relative_path, f"duplicate article id: {article.id}")
            )
        else:
            by_id[article.id] = article

    for article in articles:
        issues.extend(_validate_article(project_root, article, by_id))
    return issues


def _validate_article(
    project_root: Path,
    article: KnowledgeArticle,
    by_id: dict[str, KnowledgeArticle],
) -> list[KnowledgeIssue]:
    issues: list[KnowledgeIssue] = []
    issues.extend(_validate_article_identity(article))
    issues.extend(_validate_article_sources(project_root, article))
    issues.extend(_validate_article_related(article, by_id))
    issues.extend(_validate_article_body(article))
    return issues


def _validate_article_identity(article: KnowledgeArticle) -> list[KnowledgeIssue]:
    issues: list[KnowledgeIssue] = []
    if not _ID_RE.fullmatch(article.id):
        issues.append(KnowledgeIssue(article.relative_path, f"invalid article id: {article.id}"))
    if article.kind not in VALID_KINDS:
        issues.append(
            KnowledgeIssue(article.relative_path, f"invalid article kind: {article.kind}")
        )
    if article.status not in VALID_STATUSES:
        issues.append(
            KnowledgeIssue(article.relative_path, f"invalid article status: {article.status}")
        )
    return issues


def _validate_article_sources(
    project_root: Path, article: KnowledgeArticle
) -> list[KnowledgeIssue]:
    if not article.sources:
        return [KnowledgeIssue(article.relative_path, "sources must not be empty")]
    issues: list[KnowledgeIssue] = []
    for source in article.sources:
        source_issue = _source_issue(project_root, source)
        if source_issue:
            issues.append(KnowledgeIssue(article.relative_path, source_issue))
    return issues


def _validate_article_related(
    article: KnowledgeArticle, by_id: dict[str, KnowledgeArticle]
) -> list[KnowledgeIssue]:
    issues: list[KnowledgeIssue] = []
    for related in article.related:
        issue = _related_issue(article, related, by_id)
        if issue:
            issues.append(KnowledgeIssue(article.relative_path, issue))
    return issues


def _related_issue(
    article: KnowledgeArticle, related: str, by_id: dict[str, KnowledgeArticle]
) -> str | None:
    if related == article.id:
        return "article cannot relate to itself"
    if related not in by_id:
        return f"unknown related id: {related}"
    return None


def _validate_article_body(article: KnowledgeArticle) -> list[KnowledgeIssue]:
    if not article.body:
        return [KnowledgeIssue(article.relative_path, "article body is empty")]
    if _first_heading(article.body) != article.title:
        return [KnowledgeIssue(article.relative_path, "first H1 must match title")]
    return []


def _source_issue(project_root: Path, source: str) -> str | None:
    source_path = Path(source)
    if source_path.is_absolute():
        return f"source must be repo-relative: {source}"
    if ".." in source_path.parts:
        return f"source must not escape the repo: {source}"
    if source_path.parts[:2] == ("docs", "knowledge"):
        return f"source must point to canonical repo state, not the KB: {source}"
    candidate = project_root / source_path
    if not candidate.exists():
        return f"source does not exist: {source}"
    if not candidate.is_file():
        return f"source is not a file: {source}"
    return None


def _first_heading(body: str) -> str | None:
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# "):
            return stripped[2:].strip()
        return None
    return None


def _repo_relative(project_root: Path, path: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()
