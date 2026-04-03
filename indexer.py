"""Indexing pipeline: parse documents, chunk, embed, and store."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from scilint.knowledge.embeddings import Chunk, EmbeddingStore, embed_text

logger = logging.getLogger(__name__)

DEFAULT_STORE_DIR = Path.home() / ".cache" / "scilint" / "knowledge"
DEFAULT_STORE_PATH = DEFAULT_STORE_DIR / "embeddings.npz"


def _chunk_id(zotero_key: str, section_idx: int, para_idx: int) -> str:
    """Deterministic chunk ID from item key + section + paragraph indices."""
    raw = f"{zotero_key}:{section_idx}:{para_idx}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def chunk_document(
    doc,
    zotero_item_key: str = "",
) -> list[Chunk]:
    """Split a parsed Document into Chunk objects with metadata.

    Each paragraph becomes one chunk, tagged with section context.
    """
    from scilint.document import Document

    if not isinstance(doc, Document):
        msg = f"Expected Document, got {type(doc).__name__}"
        raise TypeError(msg)

    chunks: list[Chunk] = []
    for sec_idx, section in enumerate(doc.sections):
        for para_idx, para in enumerate(section.paragraphs):
            text = para.text.strip()
            if not text or len(text) < 20:
                continue
            chunk = Chunk(
                id=_chunk_id(zotero_item_key or str(doc.source_path), sec_idx, para_idx),
                text=text,
                metadata={
                    "zotero_item_key": zotero_item_key,
                    "section_title": section.title,
                    "section_type": section.section_type.value,
                    "source_path": str(doc.source_path),
                },
            )
            chunks.append(chunk)
    return chunks


def index_chunks(
    chunks: list[Chunk],
    store: EmbeddingStore,
    batch_size: int = 64,
) -> int:
    """Embed chunks in batches and add to the store. Returns count added."""
    if not chunks:
        return 0
    texts = [c.text for c in chunks]
    total = 0
    for i in range(0, len(texts), batch_size):
        batch_texts = texts[i : i + batch_size]
        batch_chunks = chunks[i : i + batch_size]
        vectors = embed_text(batch_texts)
        store.add(batch_chunks, vectors)
        total += len(batch_chunks)
        logger.info("Embedded %d/%d chunks", total, len(chunks))
    return total


def index_document(
    doc_path: Path,
    store: EmbeddingStore,
    zotero_item_key: str = "",
) -> int:
    """Parse a document file, chunk it, embed, and add to store."""
    from scilint.parser.dispatch import parse

    doc = parse(doc_path)
    chunks = chunk_document(doc, zotero_item_key=zotero_item_key)
    if not chunks:
        logger.warning("No chunks produced from %s", doc_path)
        return 0
    return index_chunks(chunks, store)


def get_or_create_store(store_path: Path | None = None) -> EmbeddingStore:
    """Load an existing store or create a new one."""
    path = store_path or DEFAULT_STORE_PATH
    store = EmbeddingStore(path)
    if path.with_suffix(".npz").exists():
        store.load()
        logger.info("Loaded existing store with %d chunks", len(store))
    return store
