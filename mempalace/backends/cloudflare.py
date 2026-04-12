"""Cloudflare D1 + Vectorize backend for MemPalace.

Talks to the palace-api CF Worker over HTTPS.
All ChromaDB-compatible methods are implemented so the rest of
MemPalace code works without changes.

Configuration via env vars:
  MEMPALACE_CF_API_URL  — https://mempalace-palace-api.<account>.workers.dev
  MEMPALACE_CF_API_KEY  — Bearer token set via wrangler secret
"""

import os
from typing import Any, Dict, List, Optional

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

from .base import BaseCollection


def _require_httpx():
    if not _HAS_HTTPX:
        raise ImportError(
            "httpx is required for the Cloudflare backend. "
            "Install it: pip install httpx"
        )


# Max chars per chunk sent to Workers AI for embedding.
# bge-base-en-v1.5 is 512 tokens; ~6000 chars is safe with overlap room.
CHUNK_SIZE = 6000
CHUNK_OVERLAP = 400


def _chunk_document(doc_id: str, text: str, meta: dict) -> List[dict]:
    """Split a long document into overlapping chunks.

    Each chunk gets a stable ID: <original_id>_c<n>
    Metadata carries original_id + chunk_index so results can be
    grouped or deduplicated by the caller.

    Short documents (<=CHUNK_SIZE) are returned as-is with the original ID.
    """
    if len(text) <= CHUNK_SIZE:
        return [{"id": doc_id, "document": text, "metadata": meta}]

    chunks = []
    start = 0
    idx = 0
    while start < len(text):
        end = start + CHUNK_SIZE
        chunk_text = text[start:end]
        chunk_meta = {**meta, "original_id": doc_id, "chunk_index": idx}
        chunks.append({
            "id": f"{doc_id}_c{idx}",
            "document": chunk_text,
            "metadata": chunk_meta,
        })
        idx += 1
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


class CloudflareCollection(BaseCollection):
    """BaseCollection backed by the palace-api CF Worker.

    Mirrors the ChromaDB collection interface so the rest of
    MemPalace (searcher.py, mcp_server.py, sync.py) works unchanged.
    """

    def __init__(self, api_url: str, api_key: str, timeout: float = 30.0):
        _require_httpx()
        self._url = api_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._timeout = timeout

    def _client(self) -> "httpx.Client":
        return httpx.Client(headers=self._headers, timeout=self._timeout)

    # ── Writes ──────────────────────────────────────────────────

    def add(
        self,
        *,
        documents: List[str],
        ids: List[str],
        metadatas: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Add documents. Upserts — idempotent."""
        self.upsert(documents=documents, ids=ids, metadatas=metadatas)

    def upsert(
        self,
        *,
        documents: List[str],
        ids: List[str],
        metadatas: Optional[List[Dict[str, Any]]] = None,
        _retries: int = 5,
    ) -> None:
        import time as _time
        # Expand long documents into overlapping chunks before sending.
        # Each chunk is a separate drawer with a stable ID (<id>_c<n>).
        drawers = []
        for i, (doc, doc_id) in enumerate(zip(documents, ids)):
            meta = metadatas[i] if metadatas else {}
            flat_meta = {
                "wing": meta.get("wing", "default"),
                "room": meta.get("room", "general"),
                "source_file": meta.get("source_file", ""),
            }
            for chunk in _chunk_document(doc_id, doc, flat_meta):
                drawers.append({
                    "id": chunk["id"],
                    "document": chunk["document"],
                    "wing": chunk["metadata"].get("wing", "default"),
                    "room": chunk["metadata"].get("room", "general"),
                    "source_file": chunk["metadata"].get("source_file", ""),
                })
        last_exc = None
        for attempt in range(_retries):
            try:
                with self._client() as client:
                    resp = client.post(f"{self._url}/drawers", json={"drawers": drawers})
                    resp.raise_for_status()
                return
            except Exception as exc:
                last_exc = exc
                if attempt < _retries - 1:
                    # Exponential backoff: 2s, 4s, 8s, 16s
                    # Longer waits needed for Workers AI rate limit recovery
                    wait = 2 ** (attempt + 1)
                    print(f"  Retry {attempt + 1}/{_retries - 1} after {wait}s (error: {exc})")
                    _time.sleep(wait)
        raise last_exc

    def delete(self, **kwargs: Any) -> None:
        ids = kwargs.get("ids", [])
        with self._client() as client:
            for doc_id in ids:
                resp = client.delete(f"{self._url}/drawers/{doc_id}")
                resp.raise_for_status()

    # ── Reads ───────────────────────────────────────────────────

    def query(self, **kwargs: Any) -> Dict[str, Any]:
        """Semantic search — returns ChromaDB-compatible dict shape."""
        query_texts = kwargs.get("query_texts", [])
        n_results = kwargs.get("n_results", 5)
        where = kwargs.get("where", {})

        if not query_texts:
            return {"documents": [[]], "metadatas": [[]], "distances": [[]], "ids": [[]]}

        # Parse ChromaDB-style $and filter
        wing = None
        room = None
        if "$and" in where:
            for clause in where["$and"]:
                if "wing" in clause:
                    wing = clause["wing"]
                if "room" in clause:
                    room = clause["room"]
        else:
            wing = where.get("wing")
            room = where.get("room")

        payload: Dict[str, Any] = {
            "query": query_texts[0],
            "n_results": n_results,
        }
        if wing:
            payload["wing"] = wing
        if room:
            payload["room"] = room

        with self._client() as client:
            resp = client.post(f"{self._url}/search", json=payload)
            resp.raise_for_status()
            data = resp.json()

        results = data.get("results", [])
        docs, metas, dists, ids = [], [], [], []
        for r in results:
            docs.append(r["text"])
            metas.append({
                "wing": r["wing"],
                "room": r["room"],
                "source_file": r.get("source_file", ""),
            })
            dists.append(r["distance"])
            ids.append(r["id"])

        return {
            "documents": [docs],
            "metadatas": [metas],
            "distances": [dists],
            "ids": [ids],
        }

    def get(self, **kwargs: Any) -> Dict[str, Any]:
        """Get drawers by metadata filter. Used for sync and dedup checks."""
        where = kwargs.get("where", {})
        limit = kwargs.get("limit", 500)

        params: Dict[str, Any] = {"limit": limit, "offset": 0}
        if "source_file" in where:
            params["source_file"] = where["source_file"]
        if "wing" in where:
            params["wing"] = where["wing"]
        if "room" in where:
            params["room"] = where["room"]

        all_drawers: list = []
        offset = 0

        # Paginate if needed
        while True:
            params["offset"] = offset
            with self._client() as client:
                resp = client.get(f"{self._url}/drawers", params=params)
                resp.raise_for_status()
                data = resp.json()

            batch = data.get("drawers", [])
            all_drawers.extend(batch)

            # If we hit the limit cap on the server side, keep paginating
            if len(batch) < params["limit"]:
                break
            if len(all_drawers) >= limit:
                break
            offset += len(batch)

        return {
            "ids": [d["id"] for d in all_drawers],
            "documents": [d["document"] for d in all_drawers],
            "metadatas": [{
                "wing": d["wing"],
                "room": d["room"],
                "source_file": d.get("source_file", ""),
            } for d in all_drawers],
        }

    def count(self) -> int:
        with self._client() as client:
            resp = client.get(f"{self._url}/status")
            resp.raise_for_status()
            return resp.json().get("total_drawers", 0)


class CloudflareBackend:
    """Factory for the Cloudflare D1/Vectorize backend.

    Reads config from env vars:
      MEMPALACE_CF_API_URL  — Worker URL
      MEMPALACE_CF_API_KEY  — Bearer token
    Or pass them directly.
    """

    def __init__(self, api_url: str = None, api_key: str = None):
        self._api_url = api_url or os.environ.get("MEMPALACE_CF_API_URL", "")
        self._api_key = api_key or os.environ.get("MEMPALACE_CF_API_KEY", "")

    def get_collection(
        self,
        palace_path: str = None,
        collection_name: str = None,
        create: bool = False,
    ) -> CloudflareCollection:
        if not self._api_url:
            raise ValueError(
                "MEMPALACE_CF_API_URL not set. "
                "Set it in your environment or pass api_url= to CloudflareBackend()."
            )
        return CloudflareCollection(self._api_url, self._api_key)
