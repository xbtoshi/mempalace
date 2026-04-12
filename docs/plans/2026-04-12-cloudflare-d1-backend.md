# MemPalace Cloudflare D1 Backend + Hermes Integration Plan

> **Goal:** Add a Cloudflare D1/Vectorize backend to MemPalace so one shared palace lives in the cloud, accessible from any machine and any LLM. Also integrate MemPalace natively into Hermes as a memory tool (Option B — direct Python calls, not MCP).

**Architecture:**

```
Hermes (Python) ──► CloudflareCollection (BaseCollection impl)
                         │
                         ▼
                  CF Worker API (Workers runtime)
                   ├── D1         — documents + metadata (wing/room/source/id)
                   └── Vectorize  — embeddings for semantic search
                   └── Workers AI — @cf/baai/bge-base-en-v1.5 for embeddings
```

**Migration (bidirectional):**
```
Local ChromaDB  ──► mempalace sync push  ──► CF Worker API
CF Worker API   ──► mempalace sync pull  ──► Local ChromaDB
```

**Hermes integration:**
- New tool: `mcp_palace_search` — semantic search across the palace
- New tool: `mcp_palace_store` — store a memory (drawer) into the palace
- Auto-inject relevant context at session start from palace search

**Tech Stack:**
- Python: httpx (async HTTP client) for CF backend
- CF Worker: TypeScript + Hono, D1, Vectorize, Workers AI
- Hermes: new tools/palace_tool.py + toolsets.py entry

---

## Task 1: CF Worker — project scaffold

**Objective:** Create the CF Worker that will serve as the palace API.

**Files:**
- Create: `workers/palace-api/wrangler.toml`
- Create: `workers/palace-api/src/index.ts`
- Create: `workers/palace-api/package.json`
- Create: `workers/palace-api/tsconfig.json`

**Step 1: Create directory structure**
```bash
mkdir -p ~/Projects/mempalace/workers/palace-api/src
```

**Step 2: Create wrangler.toml**
```toml
name = "mempalace-palace-api"
main = "src/index.ts"
compatibility_date = "2024-12-01"
compatibility_flags = ["nodejs_compat"]

[[d1_databases]]
binding = "DB"
database_name = "mempalace"
database_id = "REPLACE_AFTER_CF_CREATE"

[[vectorize]]
binding = "VECTORIZE"
index_name = "mempalace-drawers"

[ai]
binding = "AI"

[vars]
PALACE_API_KEY = ""  # set via wrangler secret

[[rules]]
type = "ESModule"
globs = ["**/*.ts"]
```

**Step 3: Create package.json**
```json
{
  "name": "palace-api",
  "version": "1.0.0",
  "private": true,
  "scripts": {
    "dev": "wrangler dev",
    "deploy": "wrangler deploy",
    "db:create": "wrangler d1 create mempalace",
    "db:migrate": "wrangler d1 execute mempalace --file=./schema.sql",
    "vectorize:create": "wrangler vectorize create mempalace-drawers --dimensions=768 --metric=cosine"
  },
  "devDependencies": {
    "wrangler": "^3.0.0",
    "@cloudflare/workers-types": "^4.0.0",
    "typescript": "^5.0.0"
  },
  "dependencies": {
    "hono": "^4.0.0"
  }
}
```

**Step 4: Create tsconfig.json**
```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ES2022",
    "moduleResolution": "bundler",
    "lib": ["ES2022"],
    "types": ["@cloudflare/workers-types"],
    "strict": true,
    "noEmit": true
  },
  "include": ["src/**/*"]
}
```

**Commit:**
```bash
git add workers/
git commit -m "feat: scaffold CF Worker palace-api"
```

---

## Task 2: CF Worker — D1 schema

**Objective:** Create the SQL schema for drawers stored in D1.

**Files:**
- Create: `workers/palace-api/schema.sql`

**Schema:**
```sql
CREATE TABLE IF NOT EXISTS drawers (
  id          TEXT PRIMARY KEY,
  document    TEXT NOT NULL,
  wing        TEXT NOT NULL DEFAULT 'default',
  room        TEXT NOT NULL DEFAULT 'general',
  source_file TEXT,
  source_mtime REAL,
  created_at  INTEGER NOT NULL DEFAULT (unixepoch()),
  updated_at  INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_drawers_wing ON drawers(wing);
CREATE INDEX IF NOT EXISTS idx_drawers_room ON drawers(room);
CREATE INDEX IF NOT EXISTS idx_drawers_wing_room ON drawers(wing, room);
CREATE INDEX IF NOT EXISTS idx_drawers_source ON drawers(source_file);
```

**Commit:**
```bash
git add workers/palace-api/schema.sql
git commit -m "feat: D1 schema for palace drawers"
```

---

## Task 3: CF Worker — API routes (index.ts)

**Objective:** Implement the full Worker API that handles all palace operations.

**Files:**
- Modify: `workers/palace-api/src/index.ts`

**Full implementation:**
```typescript
import { Hono } from "hono";
import { bearerAuth } from "hono/bearer-auth";

type Env = {
  DB: D1Database;
  VECTORIZE: VectorizeIndex;
  AI: Ai;
  PALACE_API_KEY: string;
};

type Metadata = {
  wing: string;
  room: string;
  source_file?: string;
};

const app = new Hono<{ Bindings: Env }>();

// Auth middleware — all routes require Bearer token
app.use("/*", async (c, next) => {
  const key = c.env.PALACE_API_KEY;
  if (key) {
    const auth = bearerAuth({ token: key });
    return auth(c, next);
  }
  return next();
});

// POST /drawers — add or upsert drawers
// Body: { drawers: [{ id, document, wing, room, source_file? }] }
app.post("/drawers", async (c) => {
  const { drawers } = await c.req.json<{
    drawers: Array<{ id: string; document: string; wing: string; room: string; source_file?: string }>;
  }>();

  if (!drawers?.length) return c.json({ error: "No drawers provided" }, 400);

  // Embed documents in batch (Workers AI max 100 per call)
  const BATCH = 100;
  const allVectors: VectorizeVector[] = [];

  for (let i = 0; i < drawers.length; i += BATCH) {
    const batch = drawers.slice(i, i + BATCH);
    const embedResp = await c.env.AI.run("@cf/baai/bge-base-en-v1.5", {
      text: batch.map((d) => d.document),
    });
    const embeddings = (embedResp as { data: number[][] }).data;

    for (let j = 0; j < batch.length; j++) {
      const d = batch[j];
      allVectors.push({
        id: d.id,
        values: embeddings[j],
        metadata: { wing: d.wing, room: d.room, source_file: d.source_file ?? "" } as Metadata,
      });
    }
  }

  // Upsert into Vectorize
  await c.env.VECTORIZE.upsert(allVectors);

  // Upsert into D1
  const stmt = c.env.DB.prepare(`
    INSERT INTO drawers (id, document, wing, room, source_file, updated_at)
    VALUES (?1, ?2, ?3, ?4, ?5, unixepoch())
    ON CONFLICT(id) DO UPDATE SET
      document=excluded.document,
      wing=excluded.wing,
      room=excluded.room,
      source_file=excluded.source_file,
      updated_at=unixepoch()
  `);

  const batch_stmts = drawers.map((d) =>
    stmt.bind(d.id, d.document, d.wing, d.room, d.source_file ?? "")
  );
  await c.env.DB.batch(batch_stmts);

  return c.json({ ok: true, count: drawers.length });
});

// POST /search — semantic search
// Body: { query, wing?, room?, n_results?, max_distance? }
app.post("/search", async (c) => {
  const { query, wing, room, n_results = 5, max_distance = 0 } = await c.req.json<{
    query: string;
    wing?: string;
    room?: string;
    n_results?: number;
    max_distance?: number;
  }>();

  if (!query) return c.json({ error: "query required" }, 400);

  // Embed query
  const embedResp = await c.env.AI.run("@cf/baai/bge-base-en-v1.5", { text: [query] });
  const queryVec = (embedResp as { data: number[][] }).data[0];

  // Build Vectorize filter
  const filter: VectorizeVectorMetadataFilter = {};
  if (wing) filter["wing"] = wing;
  if (room) filter["room"] = room;

  const results = await c.env.VECTORIZE.query(queryVec, {
    topK: n_results,
    filter: Object.keys(filter).length ? filter : undefined,
    returnMetadata: "all",
  });

  // Fetch documents from D1
  const ids = results.matches.map((m) => m.id);
  if (!ids.length) return c.json({ query, results: [] });

  const placeholders = ids.map(() => "?").join(",");
  const rows = await c.env.DB.prepare(
    `SELECT id, document, wing, room, source_file FROM drawers WHERE id IN (${placeholders})`
  )
    .bind(...ids)
    .all();

  const docMap = new Map(rows.results.map((r: any) => [r.id, r]));

  const hits = results.matches
    .map((m) => {
      const row = docMap.get(m.id) as any;
      if (!row) return null;
      const distance = 1 - m.score; // Vectorize returns cosine similarity (0-1), convert to distance
      if (max_distance > 0 && distance > max_distance) return null;
      return {
        id: m.id,
        text: row.document,
        wing: row.wing,
        room: row.room,
        source_file: row.source_file,
        similarity: Math.round(m.score * 1000) / 1000,
        distance: Math.round(distance * 10000) / 10000,
      };
    })
    .filter(Boolean);

  return c.json({ query, filters: { wing, room }, results: hits });
});

// GET /drawers — list/get drawers (for migration pull)
// Query: wing?, room?, source_file?, limit?, offset?
app.get("/drawers", async (c) => {
  const wing = c.req.query("wing");
  const room = c.req.query("room");
  const source_file = c.req.query("source_file");
  const limit = parseInt(c.req.query("limit") ?? "500");
  const offset = parseInt(c.req.query("offset") ?? "0");

  const conditions: string[] = [];
  const bindings: string[] = [];
  if (wing) { conditions.push("wing = ?"); bindings.push(wing); }
  if (room) { conditions.push("room = ?"); bindings.push(room); }
  if (source_file) { conditions.push("source_file = ?"); bindings.push(source_file); }

  const where = conditions.length ? `WHERE ${conditions.join(" AND ")}` : "";
  const rows = await c.env.DB.prepare(
    `SELECT id, document, wing, room, source_file FROM drawers ${where} LIMIT ? OFFSET ?`
  ).bind(...bindings, limit, offset).all();

  const total = await c.env.DB.prepare(
    `SELECT COUNT(*) as n FROM drawers ${where}`
  ).bind(...bindings).first<{ n: number }>();

  return c.json({ drawers: rows.results, total: total?.n ?? 0, limit, offset });
});

// DELETE /drawers/:id
app.delete("/drawers/:id", async (c) => {
  const id = c.req.param("id");
  await c.env.DB.prepare("DELETE FROM drawers WHERE id = ?").bind(id).run();
  await c.env.VECTORIZE.deleteByIds([id]);
  return c.json({ ok: true, id });
});

// GET /status — palace stats
app.get("/status", async (c) => {
  const total = await c.env.DB.prepare("SELECT COUNT(*) as n FROM drawers").first<{ n: number }>();
  const wings = await c.env.DB.prepare(
    "SELECT wing, COUNT(*) as n FROM drawers GROUP BY wing ORDER BY n DESC"
  ).all();
  return c.json({ total_drawers: total?.n ?? 0, wings: wings.results });
});

export default app;
```

**Commit:**
```bash
git add workers/palace-api/src/index.ts
git commit -m "feat: palace-api Worker with search/add/list/delete/status routes"
```

---

## Task 4: Python CloudflareBackend + CloudflareCollection

**Objective:** Implement BaseCollection for the CF backend so MemPalace Python code can swap backends with one line.

**Files:**
- Create: `mempalace/backends/cloudflare.py`

**Implementation:**
```python
"""Cloudflare D1 + Vectorize backend for MemPalace.

Talks to the palace-api CF Worker over HTTPS.
All ChromaDB-compatible methods are implemented so the rest of
MemPalace code works without changes.
"""

import os
import time
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


class CloudflareCollection(BaseCollection):
    """BaseCollection backed by the palace-api CF Worker."""

    def __init__(self, api_url: str, api_key: str, timeout: float = 30.0):
        _require_httpx()
        self._url = api_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._timeout = timeout

    def _client(self):
        return httpx.Client(headers=self._headers, timeout=self._timeout)

    def add(self, *, documents: List[str], ids: List[str], metadatas: Optional[List[Dict[str, Any]]] = None) -> None:
        """Add documents (upserts under the hood — idempotent)."""
        self.upsert(documents=documents, ids=ids, metadatas=metadatas)

    def upsert(self, *, documents: List[str], ids: List[str], metadatas: Optional[List[Dict[str, Any]]] = None) -> None:
        drawers = []
        for i, (doc, doc_id) in enumerate(zip(documents, ids)):
            meta = metadatas[i] if metadatas else {}
            drawers.append({
                "id": doc_id,
                "document": doc,
                "wing": meta.get("wing", "default"),
                "room": meta.get("room", "general"),
                "source_file": meta.get("source_file", ""),
            })
        with self._client() as client:
            resp = client.post(f"{self._url}/drawers", json={"drawers": drawers})
            resp.raise_for_status()

    def query(self, **kwargs: Any) -> Dict[str, Any]:
        """Semantic search — mirrors ChromaDB query() return shape."""
        query_texts = kwargs.get("query_texts", [])
        n_results = kwargs.get("n_results", 5)
        where = kwargs.get("where", {})

        if not query_texts:
            return {"documents": [[]], "metadatas": [[]], "distances": [[]], "ids": [[]]}

        wing = where.get("wing") or (where.get("$and", [{}])[0].get("wing") if "$and" in where else None)
        room = where.get("room") or (where.get("$and", [{}, {}])[1].get("room") if "$and" in where else None)

        query = query_texts[0]
        with self._client() as client:
            resp = client.post(f"{self._url}/search", json={
                "query": query,
                "wing": wing,
                "room": room,
                "n_results": n_results,
            })
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
        """Get drawers by metadata filter or IDs."""
        where = kwargs.get("where", {})
        limit = kwargs.get("limit", 500)

        params: Dict[str, Any] = {"limit": limit, "offset": 0}
        if "source_file" in where:
            params["source_file"] = where["source_file"]
        if "wing" in where:
            params["wing"] = where["wing"]
        if "room" in where:
            params["room"] = where["room"]

        with self._client() as client:
            resp = client.get(f"{self._url}/drawers", params=params)
            resp.raise_for_status()
            data = resp.json()

        drawers = data.get("drawers", [])
        return {
            "ids": [d["id"] for d in drawers],
            "documents": [d["document"] for d in drawers],
            "metadatas": [{
                "wing": d["wing"],
                "room": d["room"],
                "source_file": d.get("source_file", ""),
            } for d in drawers],
        }

    def delete(self, **kwargs: Any) -> None:
        ids = kwargs.get("ids", [])
        with self._client() as client:
            for doc_id in ids:
                resp = client.delete(f"{self._url}/drawers/{doc_id}")
                resp.raise_for_status()

    def count(self) -> int:
        with self._client() as client:
            resp = client.get(f"{self._url}/status")
            resp.raise_for_status()
            return resp.json().get("total_drawers", 0)


class CloudflareBackend:
    """Factory for the Cloudflare D1/Vectorize backend.

    Reads config from env vars:
      MEMPALACE_CF_API_URL  — https://mempalace-palace-api.<account>.workers.dev
      MEMPALACE_CF_API_KEY  — Bearer token (set via wrangler secret)
    Or pass them directly.
    """

    def __init__(self, api_url: str = None, api_key: str = None):
        self._api_url = api_url or os.environ.get("MEMPALACE_CF_API_URL", "")
        self._api_key = api_key or os.environ.get("MEMPALACE_CF_API_KEY", "")

    def get_collection(self, palace_path: str = None, collection_name: str = None, create: bool = False):
        if not self._api_url:
            raise ValueError(
                "MEMPALACE_CF_API_URL not set. "
                "Export it or pass api_url= to CloudflareBackend()."
            )
        return CloudflareCollection(self._api_url, self._api_key)
```

**Commit:**
```bash
git add mempalace/backends/cloudflare.py
git commit -m "feat: CloudflareCollection + CloudflareBackend implementing BaseCollection"
```

---

## Task 5: Update backends __init__.py

**Objective:** Export CloudflareBackend from the backends package.

**Files:**
- Modify: `mempalace/backends/__init__.py`

```python
"""Storage backend implementations for MemPalace."""

from .base import BaseCollection
from .chroma import ChromaBackend, ChromaCollection

try:
    from .cloudflare import CloudflareBackend, CloudflareCollection
    __all__ = ["BaseCollection", "ChromaBackend", "ChromaCollection", "CloudflareBackend", "CloudflareCollection"]
except ImportError:
    __all__ = ["BaseCollection", "ChromaBackend", "ChromaCollection"]
```

**Commit:**
```bash
git add mempalace/backends/__init__.py
git commit -m "feat: export CloudflareBackend from backends package"
```

---

## Task 6: palace.py — backend selection from env

**Objective:** Make palace.py pick backend based on MEMPALACE_BACKEND env var.

**Files:**
- Modify: `mempalace/palace.py`

Replace the `_DEFAULT_BACKEND = ChromaBackend()` block and `get_collection` function:

```python
import os

def _build_backend():
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
    """Get the palace collection through the backend layer."""
    backend = _build_backend()
    return backend.get_collection(
        palace_path,
        collection_name=collection_name,
        create=create,
    )
```

This means: set `MEMPALACE_BACKEND=cloudflare` + `MEMPALACE_CF_API_URL=...` + `MEMPALACE_CF_API_KEY=...` and everything works.

**Commit:**
```bash
git add mempalace/palace.py
git commit -m "feat: backend selection via MEMPALACE_BACKEND env var"
```

---

## Task 7: sync.py — local↔CF migration

**Objective:** Add `mempalace sync push` and `mempalace sync pull` commands.

**Files:**
- Create: `mempalace/sync.py`

```python
"""
mempalace sync — bidirectional sync between local ChromaDB palace and CF backend.

  mempalace sync push [--palace /path] [--batch 100]  — local → CF
  mempalace sync pull [--palace /path] [--batch 100]  — CF → local
"""

import os
import time

BATCH_SIZE = 100


def push(palace_path: str, batch_size: int = BATCH_SIZE, dry_run: bool = False):
    """Push local ChromaDB palace to Cloudflare."""
    from .backends.chroma import ChromaBackend
    from .backends.cloudflare import CloudflareBackend

    print(f"\n{'=' * 60}")
    print("  MemPalace Sync — Push (Local → Cloudflare)")
    print(f"{'=' * 60}\n")

    # Read all from local
    local = ChromaBackend().get_collection(palace_path, create=False)
    total = local.count()
    print(f"  Local palace: {palace_path}")
    print(f"  Total drawers: {total}")

    if dry_run:
        print("  DRY RUN — no changes.")
        return True

    # Get all docs (paginated via get with large limit)
    all_data = local.get(limit=100_000)
    ids = all_data["ids"]
    docs = all_data["documents"]
    metas = all_data["metadatas"]

    # Push to CF in batches
    cf = CloudflareBackend().get_collection()
    pushed = 0
    for i in range(0, len(ids), batch_size):
        b_ids = ids[i:i + batch_size]
        b_docs = docs[i:i + batch_size]
        b_metas = metas[i:i + batch_size]
        cf.upsert(documents=b_docs, ids=b_ids, metadatas=b_metas)
        pushed += len(b_ids)
        print(f"  Pushed {pushed}/{len(ids)} drawers...")

    print(f"\n  Done. {pushed} drawers synced to Cloudflare.")
    print(f"{'=' * 60}\n")
    return True


def pull(palace_path: str, batch_size: int = BATCH_SIZE, dry_run: bool = False):
    """Pull Cloudflare palace to local ChromaDB."""
    from .backends.chroma import ChromaBackend
    from .backends.cloudflare import CloudflareBackend, CloudflareCollection
    import httpx

    print(f"\n{'=' * 60}")
    print("  MemPalace Sync — Pull (Cloudflare → Local)")
    print(f"{'=' * 60}\n")

    cf = CloudflareBackend().get_collection()
    total_remote = cf.count()
    print(f"  Remote drawers: {total_remote}")

    if dry_run:
        print("  DRY RUN — no changes.")
        return True

    # Paginate through all remote drawers
    local = ChromaBackend().get_collection(palace_path, create=True)
    pulled = 0
    offset = 0

    # Access the underlying CF collection directly for pagination
    assert isinstance(cf, CloudflareCollection)
    import httpx as _httpx

    while True:
        with cf._client() as client:
            resp = client.get(
                f"{cf._url}/drawers",
                params={"limit": batch_size, "offset": offset}
            )
            resp.raise_for_status()
            data = resp.json()

        drawers = data.get("drawers", [])
        if not drawers:
            break

        local.upsert(
            documents=[d["document"] for d in drawers],
            ids=[d["id"] for d in drawers],
            metadatas=[{"wing": d["wing"], "room": d["room"], "source_file": d.get("source_file", "")} for d in drawers],
        )
        pulled += len(drawers)
        offset += len(drawers)
        print(f"  Pulled {pulled}/{total_remote} drawers...")

        if len(drawers) < batch_size:
            break

    print(f"\n  Done. {pulled} drawers synced to local palace.")
    print(f"{'=' * 60}\n")
    return True
```

**Add sync subcommand to cli.py** — add in the `main()` dispatch:
```python
elif cmd == "sync":
    from .sync import push, pull
    sub = args[0] if args else ""
    palace = _get_palace_path(parsed)
    dry = "--dry-run" in sys.argv
    if sub == "push":
        sync.push(palace, dry_run=dry)
    elif sub == "pull":
        sync.pull(palace, dry_run=dry)
    else:
        print("Usage: mempalace sync push|pull [--palace PATH] [--dry-run]")
```

**Commit:**
```bash
git add mempalace/sync.py
git commit -m "feat: bidirectional sync push/pull local↔cloudflare"
```

---

## Task 8: Hermes — palace_tool.py

**Objective:** Native Hermes tool that wraps MemPalace search + store, using either local or CF backend depending on env.

**Files:**
- Create: `~/Projects/claude-code/tools/palace_tool.py`

```python
"""
MemPalace native tool for Hermes.

Uses MemPalace's Python API directly (not MCP) for zero-overhead
memory search and storage across all Hermes sessions.

Configuration (in ~/.hermes/.env or environment):
  MEMPALACE_BACKEND       — "chroma" (default) or "cloudflare"
  MEMPALACE_PALACE_PATH   — path to local palace (chroma mode)
  MEMPALACE_CF_API_URL    — CF Worker URL (cloudflare mode)
  MEMPALACE_CF_API_KEY    — Bearer token (cloudflare mode)
"""

import json
import os
import sys
from pathlib import Path
from tools.registry import registry


def _is_available() -> bool:
    try:
        import mempalace  # noqa
        return True
    except ImportError:
        return False


def _get_palace_path() -> str:
    return os.environ.get(
        "MEMPALACE_PALACE_PATH",
        str(Path.home() / ".mempalace" / "palace")
    )


def palace_search(
    query: str,
    wing: str = None,
    room: str = None,
    n_results: int = 5,
    max_distance: float = 0.0,
    task_id: str = None,
) -> str:
    try:
        from mempalace.searcher import search_memories
        from mempalace.palace import get_collection

        backend = os.environ.get("MEMPALACE_BACKEND", "chroma").lower()

        if backend == "cloudflare":
            from mempalace.backends.cloudflare import CloudflareBackend
            col = CloudflareBackend().get_collection()
            # Direct CF search via the collection
            where = {}
            if wing:
                where["wing"] = wing
            if room:
                where["room"] = room
            result = col.query(
                query_texts=[query],
                n_results=n_results,
                where=where,
            )
            docs = result["documents"][0]
            metas = result["metadatas"][0]
            dists = result["distances"][0]
            hits = []
            for doc, meta, dist in zip(docs, metas, dists):
                if max_distance > 0 and dist > max_distance:
                    continue
                hits.append({
                    "text": doc,
                    "wing": meta.get("wing", "?"),
                    "room": meta.get("room", "?"),
                    "source_file": meta.get("source_file", ""),
                    "similarity": round(max(0.0, 1 - dist), 3),
                    "distance": round(dist, 4),
                })
            return json.dumps({
                "query": query,
                "filters": {"wing": wing, "room": room},
                "results": hits,
            })
        else:
            # Local chroma
            result = search_memories(
                query=query,
                palace_path=_get_palace_path(),
                wing=wing,
                room=room,
                n_results=n_results,
                max_distance=max_distance,
            )
            return json.dumps(result)

    except Exception as e:
        return json.dumps({"error": str(e), "results": []})


def palace_store(
    content: str,
    wing: str = "hermes",
    room: str = "general",
    source_id: str = None,
    task_id: str = None,
) -> str:
    try:
        import hashlib
        import time
        from mempalace.palace import get_collection
        from mempalace.config import sanitize_name, sanitize_content

        wing = sanitize_name(wing)
        room = sanitize_name(room)
        content = sanitize_content(content)

        doc_id = source_id or hashlib.sha256(
            f"{wing}:{room}:{content}:{time.time()}".encode()
        ).hexdigest()[:16]

        backend = os.environ.get("MEMPALACE_BACKEND", "chroma").lower()

        if backend == "cloudflare":
            from mempalace.backends.cloudflare import CloudflareBackend
            col = CloudflareBackend().get_collection()
        else:
            col = get_collection(_get_palace_path(), create=True)

        col.upsert(
            documents=[content],
            ids=[doc_id],
            metadatas=[{"wing": wing, "room": room, "source_file": "hermes"}],
        )

        return json.dumps({"ok": True, "id": doc_id, "wing": wing, "room": room})

    except Exception as e:
        return json.dumps({"error": str(e), "ok": False})


# Register tools
registry.register(
    name="palace_search",
    toolset="palace",
    schema={
        "name": "palace_search",
        "description": (
            "Search the MemPalace memory system for relevant context. "
            "Returns verbatim stored memories matching the query. "
            "Use this to recall facts, decisions, code patterns, or context from past sessions. "
            "Works across all LLMs and devices sharing the same palace."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query",
                },
                "wing": {
                    "type": "string",
                    "description": "Optional: filter by wing (person or project name)",
                },
                "room": {
                    "type": "string",
                    "description": "Optional: filter by room (topic category)",
                },
                "n_results": {
                    "type": "integer",
                    "description": "Max results to return (default: 5)",
                    "default": 5,
                },
                "max_distance": {
                    "type": "number",
                    "description": "Max cosine distance threshold 0-2, 0=disabled (default: 0)",
                    "default": 0.0,
                },
            },
            "required": ["query"],
        },
    },
    handler=lambda args, **kw: palace_search(
        query=args["query"],
        wing=args.get("wing"),
        room=args.get("room"),
        n_results=args.get("n_results", 5),
        max_distance=args.get("max_distance", 0.0),
        task_id=kw.get("task_id"),
    ),
    check_fn=_is_available,
    requires_env=[],
)

registry.register(
    name="palace_store",
    toolset="palace",
    schema={
        "name": "palace_store",
        "description": (
            "Store a memory into the MemPalace for future recall. "
            "Use this to preserve important facts, decisions, code patterns, "
            "or context that should be available in future sessions. "
            "Stored memories are searchable by all LLMs sharing this palace."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The verbatim content to store",
                },
                "wing": {
                    "type": "string",
                    "description": "Wing name — person or project (default: hermes)",
                    "default": "hermes",
                },
                "room": {
                    "type": "string",
                    "description": "Room name — topic category (default: general)",
                    "default": "general",
                },
                "source_id": {
                    "type": "string",
                    "description": "Optional stable ID for deduplication (upserts if same ID)",
                },
            },
            "required": ["content"],
        },
    },
    handler=lambda args, **kw: palace_store(
        content=args["content"],
        wing=args.get("wing", "hermes"),
        room=args.get("room", "general"),
        source_id=args.get("source_id"),
        task_id=kw.get("task_id"),
    ),
    check_fn=_is_available,
    requires_env=[],
)
```

**Commit:**
```bash
git add tools/palace_tool.py
git commit -m "feat: palace_search + palace_store native Hermes tools"
```

---

## Task 9: Wire palace_tool into Hermes model_tools.py + toolsets.py

**Objective:** Register the palace toolset so Hermes discovers and loads it.

**Files:**
- Modify: `~/Projects/claude-code/model_tools.py` — add import
- Modify: `~/Projects/claude-code/toolsets.py` — add palace toolset

**In model_tools.py**, add to `_discover_tools()` imports list:
```python
from tools import palace_tool  # noqa
```

**In toolsets.py**, add new toolset entry:
```python
ToolsetDef(
    name="palace",
    description="MemPalace memory — semantic search + store across sessions and LLMs",
    tools=["palace_search", "palace_store"],
    optional_env=["MEMPALACE_CF_API_URL", "MEMPALACE_CF_API_KEY", "MEMPALACE_PALACE_PATH"],
),
```

**Commit:**
```bash
git add model_tools.py toolsets.py
git commit -m "feat: register palace toolset in Hermes"
```

---

## Task 10: CF Worker deployment instructions (README)

**Objective:** Document how to deploy and configure.

**Files:**
- Create: `workers/palace-api/README.md`

```markdown
# MemPalace Palace API — Cloudflare Worker

## First-time setup

```bash
cd workers/palace-api
npm install

# Create D1 database
wrangler d1 create mempalace
# Copy the database_id from output and paste into wrangler.toml

# Run the schema migration
wrangler d1 execute mempalace --file=./schema.sql

# Create Vectorize index (768 dims = bge-base-en-v1.5)
wrangler vectorize create mempalace-drawers --dimensions=768 --metric=cosine

# Set API key secret
wrangler secret put PALACE_API_KEY
# Enter a strong random key when prompted

# Deploy
wrangler deploy
```

## Environment variables for Python clients

```bash
export MEMPALACE_BACKEND=cloudflare
export MEMPALACE_CF_API_URL=https://mempalace-palace-api.<your-subdomain>.workers.dev
export MEMPALACE_CF_API_KEY=<your-secret-key>
```

## Hermes integration

Add to `~/.hermes/.env`:
```
MEMPALACE_BACKEND=cloudflare
MEMPALACE_CF_API_URL=https://mempalace-palace-api.<your-subdomain>.workers.dev
MEMPALACE_CF_API_KEY=<your-secret-key>
```

Enable the toolset:
```
hermes tools enable palace
```

## Migrating existing local palace to CF

```bash
export MEMPALACE_CF_API_URL=...
export MEMPALACE_CF_API_KEY=...
mempalace sync push --palace ~/.mempalace/palace
```
```

**Commit:**
```bash
git add workers/palace-api/README.md
git commit -m "docs: CF Worker setup and Hermes integration guide"
```

---

## Execution order

Tasks 1–7 are in the MemPalace fork (`~/Projects/mempalace`).
Tasks 8–10 are in Hermes (`~/Projects/claude-code`).

Tasks 1–3 (Worker scaffold+schema+routes) can be done in parallel with Tasks 4–6 (Python backend).
Task 7 (sync) depends on Tasks 4–6.
Tasks 8–9 (Hermes) depend on Tasks 4–6 being done.
Task 10 (docs) can be done anytime.
