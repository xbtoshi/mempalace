"""
palace.py — Shared palace operations.

Consolidates collection access patterns used by both miners and the MCP server.

Backend selection via MEMPALACE_BACKEND env var:
  "chroma"      — local ChromaDB (default)
  "cloudflare"  — CF D1 + Vectorize via palace-api Worker
                  requires: MEMPALACE_CF_API_URL, MEMPALACE_CF_API_KEY
"""

import os

SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    ".next",
    "coverage",
    ".mempalace",
    ".ruff_cache",
    ".mypy_cache",
    ".pytest_cache",
    ".cache",
    ".tox",
    ".nox",
    ".idea",
    ".vscode",
    ".ipynb_checkpoints",
    ".eggs",
    "htmlcov",
    "target",
}


def _build_backend():
    """Instantiate the configured backend."""
    backend = os.environ.get("MEMPALACE_BACKEND", "chroma").lower()
    if backend == "cloudflare":
        from .backends.cloudflare import CloudflareBackend
        return CloudflareBackend()
    else:
        from .backends.chroma import ChromaBackend
        return ChromaBackend()


def get_collection(
    palace_path: str,
    collection_name: str = "mempalace_drawers",
    create: bool = True,
):
    """Get the palace collection through the configured backend."""
    return _build_backend().get_collection(
        palace_path,
        collection_name=collection_name,
        create=create,
    )


def file_already_mined(collection, source_file: str, check_mtime: bool = False) -> bool:
    """Check if a file has already been filed in the palace.

    When check_mtime=True (used by project miner), returns False if the file
    has been modified since it was last mined, so it gets re-mined.
    When check_mtime=False (used by convo miner), just checks existence.
    """
    try:
        results = collection.get(where={"source_file": source_file}, limit=1)
        if not results.get("ids"):
            return False
        if check_mtime:
            stored_meta = results.get("metadatas", [{}])[0]
            stored_mtime = stored_meta.get("source_mtime")
            if stored_mtime is None:
                return False
            current_mtime = os.path.getmtime(source_file)
            return abs(float(stored_mtime) - current_mtime) < 0.001
        return True
    except Exception:
        return False
