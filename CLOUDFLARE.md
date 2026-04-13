# MemPalace × Cloudflare

Run your palace in the cloud — shared across machines, LLMs, and devices.
One palace. Every conversation. Everywhere.

```
Your Mac  ──┐
            ├──► CF Worker (D1 + Vectorize + Workers AI) ◄──── Hermes
Anton's Mac ┘
```

---

## How it works

The Cloudflare backend replaces local ChromaDB with three CF primitives:

| Local | Cloudflare |
|-------|-----------|
| ChromaDB (documents + metadata) | D1 (SQLite) |
| ChromaDB (vector index) | Vectorize (ANN search) |
| sentence-transformers (local embed) | Workers AI `@cf/baai/bge-base-en-v1.5` |

The Python `CloudflareCollection` implements the same `BaseCollection` interface as
ChromaDB — the rest of MemPalace (search, MCP server, Hermes tools) works unchanged.
Switch backends with one env var: `MEMPALACE_BACKEND=cloudflare`.

---

## Requirements

- Node.js + npm
- [Wrangler CLI](https://developers.cloudflare.com/workers/wrangler/install-and-update/): `npm install -g wrangler`
- Python 3.9+ with `mempalace` installed from this fork
- `httpx`: `pip install httpx`
- A Cloudflare account (free tier is sufficient)

---

## First-time setup

### Option A — Automated (recommended)

```bash
cd workers/palace-api
./setup.sh
```

The script handles everything: creates D1 + Vectorize, deploys the Worker,
prompts for your API key, then imports your existing local palace.

**Flags:**
```bash
./setup.sh --skip-deploy          # Worker already deployed, import only
./setup.sh --palace ~/my/palace   # custom local palace path
./setup.sh --dry-run              # preview import, no changes
```

### Option B — Manual

```bash
cd workers/palace-api
npm install

# 1. Create D1 database — copy the database_id into wrangler.toml
wrangler d1 create mempalace

# 2. Apply schema
wrangler d1 execute mempalace --file=./schema.sql

# 3. Create Vectorize index (768 dims = bge-base-en-v1.5, cosine distance)
wrangler vectorize create mempalace-drawers --dimensions=768 --metric=cosine

# 4. Set API secret (keep this safe — it's your palace's access key)
wrangler secret put PALACE_API_KEY

# 5. Deploy
wrangler deploy
```

---

## Environment variables

Set these in your shell or `~/.hermes/.env`:

```bash
MEMPALACE_BACKEND=cloudflare
MEMPALACE_CF_API_URL=https://mempalace-palace-api.<your-subdomain>.workers.dev
MEMPALACE_CF_API_KEY=<the-secret-you-set-above>

# Optional — local palace path for sync commands (default: ~/.mempalace/palace)
MEMPALACE_PALACE_PATH=~/.mempalace/palace
```

---

## Importing your existing local palace

### Step 1 — Fix ChromaDB version mismatch (if needed)

If your palace was created with a different ChromaDB version you'll see
`KeyError: '_type'` when sync tries to read it. Check first:

```bash
mempalace migrate --dry-run
```

If it says "Palace is NOT readable" — run the migration before pushing:

```bash
mempalace migrate
```

This reads your palace directly from SQLite, rebuilds it with your current
ChromaDB version, and preserves all drawer IDs and metadata. It backs up
your old palace to `~/.mempalace/palace.pre-migrate.<timestamp>` first.

> With 23k+ drawers this takes a few minutes. Let it finish completely
> before pushing to CF.

### Step 2 — Deploy the Worker with the correct batch size

If you already deployed the Worker, redeploy after pulling the latest code
to get the reduced embed batch size (25 instead of 100 — avoids Workers AI
timeouts on free tier):

```bash
cd workers/palace-api
git pull origin feature/cloudflare-d1-backend
wrangler deploy
```

### Step 3 — Push

```bash
# Dry run first — see what would be pushed
mempalace --palace ~/.mempalace/palace sync push --dry-run

# Push for real
mempalace --palace ~/.mempalace/palace sync push
```

The push is **resumable** — state is checkpointed every 500 drawers to
`~/.mempalace/sync_state.json`. If it crashes mid-run, just re-run the
same command. It will skip everything already synced and continue from
where it left off:

```
Local drawers: 23420
Unchanged (skip): 2600     ← already on CF, skipped
To push: 20820             ← only the remainder
```

Transient Worker errors (500, timeout) are retried automatically up to
3 times with exponential backoff before failing.

---

## Keeping in sync

### Manual

```bash
mempalace sync push   # local → CF (only changed drawers)
mempalace sync pull   # CF → local (only new/changed on CF)
```

### Background daemon

```bash
# Sync every 5 minutes (default)
mempalace sync daemon

# Custom interval
mempalace sync daemon --interval 15
```

Run the daemon in a tmux session or as a launchd/systemd service to keep
your local palace and CF in continuous sync.

### Conflict resolution

The sync uses content hashes to detect what actually changed:

| Situation | Result |
|-----------|--------|
| Same content on both sides | Skip (no-op) |
| Only local changed | Push local → CF |
| Only CF changed | Pull CF → local |
| Both changed since last sync | **CF wins** (shared source of truth) |

Conflicting local versions are never discarded — they're logged to
`~/.mempalace/sync_conflicts.jsonl` for review.

Sync state is tracked in `~/.mempalace/sync_state.json`.

---

## Hermes integration

MemPalace is integrated natively into Hermes as two tools:

| Tool | Description |
|------|-------------|
| `palace_search` | Semantic search across the palace |
| `palace_store` | Store a memory drawer |

### Enable

```bash
# In a Hermes session:
/tools enable palace
```

Or add to `~/.hermes/config.yaml`:
```yaml
enabled_toolsets:
  - palace
```

### Configure

Add to `~/.hermes/.env`:
```
MEMPALACE_BACKEND=cloudflare
MEMPALACE_CF_API_URL=https://mempalace-palace-api.<your-subdomain>.workers.dev
MEMPALACE_CF_API_KEY=<your-secret>
```

---

## Worker API reference

All endpoints require `Authorization: Bearer <PALACE_API_KEY>`.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/drawers` | Upsert drawers — embeds via Workers AI, stores in Vectorize + D1 |
| `POST` | `/search` | Semantic ANN search |
| `GET` | `/drawers` | Paginated list of all drawers (used by sync pull) |
| `DELETE` | `/drawers/:id` | Delete a single drawer |
| `GET` | `/status` | Palace stats (total drawers + wing breakdown) |

### POST /drawers

```json
{
  "drawers": [
    {
      "id": "abc123",
      "document": "verbatim text content",
      "wing": "my-project",
      "room": "architecture",
      "source_file": "optional"
    }
  ]
}
```

### POST /search

```json
{
  "query": "why did we switch to Monero",
  "wing": "kyc-rip",
  "room": "payments",
  "n_results": 5,
  "max_distance": 0.5
}
```

---

## Cost estimate (Cloudflare free tier)

| Service | Free tier | Typical usage |
|---------|-----------|---------------|
| Workers | 100k req/day | Well within free |
| D1 | 5 GB storage, 5M rows/day reads | Free for personal use |
| Vectorize | 30M queried vector dimensions/month | Free for personal use |
| Workers AI | 10k neurons/day | Free for light use |

Effectively **$0/month** for personal use.

---

## Troubleshooting

**`D1_ERROR: no such table: drawers`**
The schema wasn't applied. Run:
```bash
wrangler d1 execute mempalace --file=./schema.sql
```

**`KeyError: '_type'` during sync push**
ChromaDB version mismatch. Run `mempalace migrate` first to rebuild the
local palace, then re-run the push. See "Importing your existing local
palace → Step 1" above.

**Worker returns 401**
Wrong API key. Check `MEMPALACE_CF_API_KEY` matches the secret you set with
`wrangler secret put PALACE_API_KEY`.

**Worker returns 500**
Missing D1/Vectorize binding or schema not applied. Check:
```bash
wrangler tail mempalace-palace-api
```

**Sync state reset**
If you want to force a full re-sync (re-push everything):
```bash
rm ~/.mempalace/sync_state.json
mempalace sync push
```

## Staying in sync with upstream MemPalace

This CF backend lives on `feature/cloudflare-d1-backend` in the xbtoshi/mempalace fork.
Upstream (MemPalace/mempalace) is a local-first project — this backend is not planned
for upstream merge. To pull upstream fixes/features into the fork:

```bash
# Syncs develop to upstream/develop (fast-forward)
# and reports how far this feature branch drifts
./scripts/sync-upstream.sh

# Also rebases feature/cloudflare-d1-backend onto upstream/develop
# (may hit conflicts — upstream refactored the backend seam in #413)
./scripts/sync-upstream.sh --rebase-feature
```

A GitHub Action (`.github/workflows/sync-upstream.yml`) runs the develop sync weekly
and reports feature-branch drift. Run it manually via Actions → "Sync fork with upstream"
→ Run workflow.
