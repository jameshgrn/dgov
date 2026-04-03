"""RAG pipeline combining Zotero, embeddings, and LLM provider."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scilint.knowledge.embeddings import SearchResult
    from scilint.knowledge.zotero_client import ZoteroClient

logger = logging.getLogger(__name__)


@dataclass
class RAGResult:
    """Result from a RAG query."""

    answer: str
    sources: list[str]
    confidence: float


@dataclass
class CitedPassage:
    """A retrieved passage with citation metadata."""

    text: str
    score: float
    zotero_item_key: str = ""
    section_title: str = ""
    section_type: str = ""
    page_number: str = ""
    citation: str = ""
    citekey: str = ""
    bibtex: str = ""
    extra_metadata: dict[str, str] = field(default_factory=dict)


def _build_citation(authors: list[str], year: str) -> str:
    """Build a short citation string like 'Seal et al. (1997)'."""
    if not authors:
        return f"({year})" if year else ""
    first_last = authors[0].split(",")[0].strip() if "," in authors[0] else authors[0]
    if len(authors) == 1:
        return f"{first_last} ({year})" if year else first_last
    if len(authors) == 2:
        second_last = authors[1].split(",")[0].strip() if "," in authors[1] else authors[1]
        return f"{first_last} & {second_last} ({year})" if year else f"{first_last} & {second_last}"
    return f"{first_last} et al. ({year})" if year else f"{first_last} et al."


def _build_citekey(authors: list[str], year: str) -> str:
    """Build a citekey like 'Seal1997'."""
    if not authors:
        return f"Unknown{year}"
    first_last = authors[0].split(",")[0].strip() if "," in authors[0] else authors[0]
    return f"{first_last}{year}"


class RAGPipeline:
    """Retrieval-augmented generation pipeline.

    Combines an EmbeddingStore for vector search, an optional ZoteroClient
    for citation metadata, and an optional LLM provider for answer generation.
    """

    def __init__(
        self,
        store: object,
        zotero: ZoteroClient | None = None,
        provider: object | None = None,
    ) -> None:
        self._store = store
        self._zotero = zotero
        self._provider = provider

    def retrieve(self, question: str, k: int = 5) -> list[SearchResult]:
        """Retrieve relevant chunks from the embedding store.

        Embeds the question using sentence-transformers, then searches
        the store by cosine similarity. Returns top-k SearchResult items.
        """
        from scilint.knowledge.embeddings import EmbeddingStore, embed_text

        store: EmbeddingStore = self._store  # type: ignore[assignment]
        if len(store) == 0:
            return []
        query_vec = embed_text([question])[0]
        return store.search(query_vec, k=k)

    def retrieve_with_citations(self, question: str, k: int = 5) -> list[CitedPassage]:
        """Retrieve passages and enrich with Zotero citation metadata."""
        results = self.retrieve(question, k=k)
        passages: list[CitedPassage] = []
        for r in results:
            meta = r.chunk.metadata
            item_key = meta.get("zotero_item_key", "")

            passage = CitedPassage(
                text=r.chunk.text,
                score=r.score,
                zotero_item_key=item_key,
                section_title=meta.get("section_title", ""),
                section_type=meta.get("section_type", ""),
                page_number=meta.get("page_number", ""),
            )

            if item_key and self._zotero:
                try:
                    item = self._zotero.get_item(item_key)
                    if item:
                        passage.citation = _build_citation(item.authors, item.year)
                        passage.citekey = _build_citekey(item.authors, item.year)
                except Exception:
                    logger.warning("Failed to fetch Zotero metadata for %s", item_key)

            passages.append(passage)
        return passages

    def augmented_prompt(self, question: str, context_chunks: list[str]) -> list[dict[str, str]]:
        """Build a chat-style prompt with retrieved context and the user question."""
        context_text = "\n".join(f"{i + 1}. {chunk}" for i, chunk in enumerate(context_chunks))
        return [
            {
                "role": "system",
                "content": (
                    f"Use the following context to answer the question.\n\nContext:\n{context_text}"
                ),
            },
            {"role": "user", "content": question},
        ]

    def query(self, question: str, k: int = 5) -> RAGResult:
        """End-to-end RAG: retrieve context, build prompt, generate answer.

        Requires a provider for generation (embedding is handled locally).
        """
        if self._provider is None:
            msg = (
                "Provider is required for query(). "
                "Use retrieve() or augmented_prompt() without a provider."
            )
            raise ValueError(msg)
        results = self.retrieve(question, k=k)
        chunk_texts = [r.chunk.text for r in results]
        messages = self.augmented_prompt(question, chunk_texts)
        completion = self._provider.complete(messages)  # type: ignore[union-attr]
        sources = [r.chunk.id for r in results]
        return RAGResult(answer=completion.text, sources=sources, confidence=1.0)
