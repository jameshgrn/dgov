"""Local vector store for document chunks using numpy cosine similarity."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class Chunk:
    """A text chunk with metadata for embedding."""

    id: str
    text: str
    metadata: dict[str, str]


@dataclass
class SearchResult:
    """A search result pairing a chunk with its similarity score."""

    chunk: Chunk
    score: float


class EmbeddingStore:
    """Stores document chunk embeddings with numpy-based cosine similarity search.

    Uses .npz for vectors and .jsonl sidecar for chunk metadata.
    """

    def __init__(self, store_path: Path) -> None:
        self._store_path = store_path
        self._chunks: list[Chunk] = []
        self._vectors: np.ndarray | None = None

    def add(self, chunks: list[Chunk], vectors: np.ndarray) -> None:
        """Add chunks and their corresponding embedding vectors."""
        if len(chunks) != vectors.shape[0]:
            msg = f"chunks length {len(chunks)} != vectors rows {vectors.shape[0]}"
            raise ValueError(msg)
        self._chunks.extend(chunks)
        if self._vectors is None:
            self._vectors = vectors.astype(np.float64)
        else:
            self._vectors = np.vstack([self._vectors, vectors.astype(np.float64)])

    def search(self, query_vector: np.ndarray, k: int = 5) -> list[SearchResult]:
        """Search for the k most similar chunks by cosine similarity."""
        if self._vectors is None or len(self._chunks) == 0:
            return []

        query = query_vector.astype(np.float64).flatten()
        query_norm = np.linalg.norm(query)
        if query_norm == 0:
            return []
        query_normalized = query / query_norm

        norms = np.linalg.norm(self._vectors, axis=1, keepdims=True)
        # Avoid division by zero for zero-norm vectors
        norms = np.where(norms == 0, 1.0, norms)
        vectors_normalized = self._vectors / norms

        similarities = vectors_normalized @ query_normalized

        # Get top-k indices sorted by descending similarity
        k = min(k, len(self._chunks))
        top_indices = np.argsort(similarities)[::-1][:k]

        return [
            SearchResult(chunk=self._chunks[i], score=float(similarities[i])) for i in top_indices
        ]

    def save(self) -> None:
        """Persist vectors to .npz and chunk metadata to .jsonl sidecar."""
        self._store_path.parent.mkdir(parents=True, exist_ok=True)

        if self._vectors is not None:
            np.savez_compressed(self._store_path, vectors=self._vectors)

        jsonl_path = self._store_path.with_suffix(".jsonl")
        with open(jsonl_path, "w") as f:
            for chunk in self._chunks:
                record = {"id": chunk.id, "text": chunk.text, "metadata": chunk.metadata}
                f.write(json.dumps(record) + "\n")

    def load(self) -> None:
        """Load vectors from .npz and chunk metadata from .jsonl sidecar."""
        npz_path = self._store_path
        # numpy savez_compressed adds .npz if not present
        if npz_path.suffix != ".npz":
            npz_path = npz_path.with_suffix(".npz")
        if npz_path.exists():
            data = np.load(npz_path)
            self._vectors = data["vectors"]
        else:
            self._vectors = None

        jsonl_path = self._store_path.with_suffix(".jsonl")
        self._chunks = []
        if jsonl_path.exists():
            with open(jsonl_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    record = json.loads(line)
                    self._chunks.append(
                        Chunk(
                            id=record["id"],
                            text=record["text"],
                            metadata=record["metadata"],
                        )
                    )

    def __len__(self) -> int:
        return len(self._chunks)


_MODEL_CACHE: dict[str, object] = {}

DEFAULT_MODEL = "all-MiniLM-L6-v2"


def _get_model(model_name: str = DEFAULT_MODEL):
    """Lazily load a SentenceTransformer model (cached after first call)."""
    if model_name in _MODEL_CACHE:
        return _MODEL_CACHE[model_name]
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        msg = (
            "sentence-transformers is required for embeddings. "
            "Install it with: pip install scilint[knowledge]"
        )
        raise ImportError(msg) from None
    logger.info("Loading embedding model %s (first use may download ~80MB)...", model_name)
    model = SentenceTransformer(model_name)
    _MODEL_CACHE[model_name] = model
    return model


def embed_text(texts: list[str], model_name: str = DEFAULT_MODEL) -> np.ndarray:
    """Embed a list of texts using sentence-transformers. Returns (N, dim) array."""
    if not texts:
        return np.empty((0, 0))
    model = _get_model(model_name)
    vectors = model.encode(texts, show_progress_bar=False, convert_to_numpy=True)
    return np.asarray(vectors, dtype=np.float64)
