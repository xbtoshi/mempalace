# MemPalace Palace API — Cloudflare Worker

A lightweight Cloudflare Worker that exposes a REST API over D1 (SQLite) + Vectorize (vector search) + Workers AI (embeddings). This lets you run MemPalace as a shared cloud palace — accessible from any machine, any LLM, any device.

## Architecture

```
Client (Python / Hermes)
  └── HTTPS → palace-api Worker
               ├── Workers AI  — embed text (@cf/baai/bge-base-en-v1.5, 768 dims)
               ├── Vectorize   — ANN search (cosine similarity)
               └── D1          — store documents + metadata (wing/room/source)
```

## First-time setup

```bash
cd workers/palace-api
npm install

# 1. Create D1 database
wrangler d1 create mempalace
# Copy the database_id from the output and paste it into wrangler.toml

# 2. Run the schema migration
wrangler d1 execute mempalace --file=./schema.sql

# 3. Create Vectorize index (768 dims = bge-base-en-v1.5)
wrangler vectorize create mempalace-drawers --dimensions=768 --metric=cosine

# 4. Set API key secret (strong random string)
wrangler secret put PALACE_API_KEY

# 5. Deploy
wrangler deploy
```

After deploy you'll get a URL like:
`https://mempalace-palace-api.<your-subdomain>.workers.dev`

## Configuring Python clients

Set these in your environment or `~/.hermes/.env`:

```bash
MEMPALACE_BACKEND=cloudflare
MEMPALACE_CF_API_URL=https://mempalace-palace-api.<your-subdomain>.workers.dev
MEMPALACE_CF_API_KEY=<the-secret-you-set-above>
```

## Using with Hermes

Enable the palace toolset:
```
/tools enable palace
```

Then use `palace_search` and `palace_store` tools in any Hermes session.

## Migrating your existing local palace to Cloudflare

```bash
# Set CF env vars first, then:
mempalace sync push --palace ~/.mempalace/palace

# Verify
curl -s -H "Authorization: Bearer $MEMPALACE_CF_API_KEY" \
  $MEMPALACE_CF_API_URL/status | python3 -m json.tool
```

## Pulling CF palace back to local (backup / offline use)

```bash
mempalace sync pull --palace ~/.mempalace/palace
```

## API reference

| Method | Path | Description |
|--------|------|-------------|
| POST | /drawers | Upsert drawers (embed + store) |
| POST | /search | Semantic search |
| GET | /drawers | Paginated list (for sync pull) |
| DELETE | /drawers/:id | Delete a drawer |
| GET | /status | Palace stats |

All endpoints require `Authorization: Bearer <PALACE_API_KEY>` header.

## Cost estimate

At 50k drawers (typical heavy user):
- D1: free tier (5GB)
- Vectorize: ~$0.01/month storage + negligible query cost
- Workers AI embeddings: ~$0.002 per 1k embed calls
- Workers: free tier (100k req/day)

Effectively free for personal use.
