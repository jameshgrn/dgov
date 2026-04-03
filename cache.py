"""Cache management for scilint structure and equation caches."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

_DEFAULT_CACHE_ROOT = Path.home() / ".cache" / "scilint"


def _resolve_cache_root() -> Path:
    env = os.environ.get("SCILINT_CACHE_DIR")
    if env:
        return Path(env)
    return _DEFAULT_CACHE_ROOT


def _derive_paths(root: Path) -> None:
    """Update all module-level path globals from *root*."""
    global CACHE_ROOT, STRUCTURE_DIR, PDF_TEXT_DIR, EXTRACTIONS_DIR
    global EQUATIONS_DIR, IMAGE_CACHE_DIR, FINDINGS_DIR, MANIFEST_PATH
    CACHE_ROOT = root
    STRUCTURE_DIR = root / "structure"
    PDF_TEXT_DIR = STRUCTURE_DIR / "pdf_text"
    EXTRACTIONS_DIR = STRUCTURE_DIR / "extractions"
    EQUATIONS_DIR = root / "equations"
    IMAGE_CACHE_DIR = STRUCTURE_DIR / "images"
    FINDINGS_DIR = root / "findings"
    MANIFEST_PATH = STRUCTURE_DIR / "manifest.json"


CACHE_ROOT: Path
STRUCTURE_DIR: Path
PDF_TEXT_DIR: Path
EXTRACTIONS_DIR: Path
EQUATIONS_DIR: Path
IMAGE_CACHE_DIR: Path
FINDINGS_DIR: Path
MANIFEST_PATH: Path
_derive_paths(_resolve_cache_root())


def configure_cache_root(root: Path | str) -> None:
    """Reconfigure all cache paths from a new root directory.

    Call this after loading scilint.toml if ``cache_dir`` is set.
    Must be called before any cache I/O occurs.
    """
    _derive_paths(Path(root))


def load_manifest() -> dict[str, dict]:
    """Load the cache manifest, or return empty dict."""
    if not MANIFEST_PATH.exists():
        return {}
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_manifest(manifest: dict[str, dict]) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def record_entry(key: str, source_path: str, model_id: str) -> None:
    """Record a cache entry in the manifest."""
    manifest = load_manifest()
    manifest[key] = {
        "source_path": str(source_path),
        "model_id": model_id,
        "timestamp": time.time(),
    }
    _save_manifest(manifest)


def list_entries() -> list[dict]:
    """List all cached entries with metadata."""
    manifest = load_manifest()
    entries = []
    for key, meta in manifest.items():
        doc_path = STRUCTURE_DIR / f"{key}.json"
        size = doc_path.stat().st_size if doc_path.exists() else 0
        entries.append(
            {
                "key": key,
                "source_path": meta.get("source_path", "unknown"),
                "model_id": meta.get("model_id", "unknown"),
                "timestamp": meta.get("timestamp", 0),
                "size": size,
            }
        )
    return entries


def clear_all() -> int:
    """Remove all cache directories. Returns count of files removed."""
    count = 0
    for d in (STRUCTURE_DIR, EQUATIONS_DIR):
        if d.exists():
            for f in d.rglob("*"):
                if f.is_file():
                    count += 1
            shutil.rmtree(d)
    return count


def clear_for_path(source: Path) -> int:
    """Remove cache entries for a specific source file. Returns count removed."""
    source_str = str(source)
    manifest = load_manifest()
    keys_to_remove = [
        k
        for k, v in manifest.items()
        if v.get("source_path") == source_str or v.get("source_path") == str(source.resolve())
    ]
    if not keys_to_remove:
        return 0
    count = 0
    for key in keys_to_remove:
        for candidate in (
            STRUCTURE_DIR / f"{key}.json",
            PDF_TEXT_DIR / f"{key}.txt",
            EXTRACTIONS_DIR / f"{key}.json",
        ):
            if candidate.exists():
                candidate.unlink()
                count += 1
        del manifest[key]
    _save_manifest(manifest)
    return count
