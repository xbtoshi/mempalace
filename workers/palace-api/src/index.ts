import { Hono } from "hono";
import { bearerAuth } from "hono/bearer-auth";

type Env = {
  DB: D1Database;
  VECTORIZE: VectorizeIndex;
  AI: Ai;
  PALACE_API_KEY: string;
};

type DrawerRow = {
  id: string;
  document: string;
  wing: string;
  room: string;
  source_file: string;
};

const app = new Hono<{ Bindings: Env }>();

// Auth middleware — all routes require Bearer token if key is set
app.use("/*", async (c, next) => {
  const key = c.env.PALACE_API_KEY;
  if (key) {
    return bearerAuth({ token: key })(c, next);
  }
  return next();
});

// ── POST /drawers ────────────────────────────────────────────────
// Upsert one or more drawers. Embeds via Workers AI, stores in
// Vectorize + D1.
// Body: { drawers: [{ id, document, wing, room, source_file? }] }
app.post("/drawers", async (c) => {
  const body = await c.req.json<{
    drawers: Array<{
      id: string;
      document: string;
      wing: string;
      room: string;
      source_file?: string;
    }>;
  }>();

  const drawers = body?.drawers;
  if (!drawers?.length) return c.json({ error: "No drawers provided" }, 400);

  // Embed in batches of 25 — Workers AI free tier can timeout on larger batches
  const EMBED_BATCH = 25;
  const allVectors: VectorizeVector[] = [];

  // Documents are pre-chunked by the Python client (max 6000 chars each).
  // No truncation needed here — if a document somehow exceeds the limit,
  // slice as a safety net only.
  const EMBED_MAX_CHARS = 8000;

  for (let i = 0; i < drawers.length; i += EMBED_BATCH) {
    const batch = drawers.slice(i, i + EMBED_BATCH);
    const embedResp = (await c.env.AI.run("@cf/baai/bge-base-en-v1.5", {
      text: batch.map((d) => d.document.slice(0, EMBED_MAX_CHARS)),
    })) as { data: number[][] };

    for (let j = 0; j < batch.length; j++) {
      allVectors.push({
        id: batch[j].id,
        values: embedResp.data[j],
        metadata: {
          wing: batch[j].wing,
          room: batch[j].room,
          source_file: batch[j].source_file ?? "",
        },
      });
    }
  }

  // Upsert into Vectorize
  await c.env.VECTORIZE.upsert(allVectors);

  // Upsert into D1 (batch prepared statements)
  const stmt = c.env.DB.prepare(`
    INSERT INTO drawers (id, document, wing, room, source_file, updated_at)
    VALUES (?1, ?2, ?3, ?4, ?5, unixepoch())
    ON CONFLICT(id) DO UPDATE SET
      document    = excluded.document,
      wing        = excluded.wing,
      room        = excluded.room,
      source_file = excluded.source_file,
      updated_at  = unixepoch()
  `);

  const d1Batch = drawers.map((d) =>
    stmt.bind(d.id, d.document, d.wing, d.room, d.source_file ?? "")
  );

  // D1 batch limit is 100
  for (let i = 0; i < d1Batch.length; i += 100) {
    await c.env.DB.batch(d1Batch.slice(i, i + 100));
  }

  return c.json({ ok: true, count: drawers.length });
});

// ── POST /search ────────────────────────────────────────────────
// Semantic search via Vectorize + document fetch from D1.
// Body: { query, wing?, room?, n_results?, max_distance? }
app.post("/search", async (c) => {
  const {
    query,
    wing,
    room,
    n_results = 5,
    max_distance = 0,
  } = await c.req.json<{
    query: string;
    wing?: string;
    room?: string;
    n_results?: number;
    max_distance?: number;
  }>();

  if (!query) return c.json({ error: "query required" }, 400);

  // Embed query — truncate to same limit as stored documents
  const embedResp = (await c.env.AI.run("@cf/baai/bge-base-en-v1.5", {
    text: [query.slice(0, 8000)],
  })) as { data: number[][] };
  const queryVec = embedResp.data[0];

  // Build Vectorize metadata filter
  type Filter = Record<string, string>;
  const filter: Filter = {};
  if (wing) filter["wing"] = wing;
  if (room) filter["room"] = room;

  const vectorResults = await c.env.VECTORIZE.query(queryVec, {
    topK: n_results,
    filter: Object.keys(filter).length ? filter : undefined,
    returnMetadata: "all",
  });

  const ids = vectorResults.matches.map((m) => m.id);
  if (!ids.length) return c.json({ query, filters: { wing, room }, results: [] });

  // Fetch documents from D1
  const placeholders = ids.map(() => "?").join(",");
  const { results: rows } = await c.env.DB.prepare(
    `SELECT id, document, wing, room, source_file FROM drawers WHERE id IN (${placeholders})`
  )
    .bind(...ids)
    .all<DrawerRow>();

  const docMap = new Map(rows.map((r) => [r.id, r]));

  const hits = vectorResults.matches
    .map((m) => {
      const row = docMap.get(m.id);
      if (!row) return null;
      // Vectorize cosine returns similarity (1=identical). Convert to distance.
      const similarity = m.score;
      const distance = 1 - similarity;
      if (max_distance > 0 && distance > max_distance) return null;
      return {
        id: m.id,
        text: row.document,
        wing: row.wing,
        room: row.room,
        source_file: row.source_file,
        similarity: Math.round(similarity * 1000) / 1000,
        distance: Math.round(distance * 10000) / 10000,
      };
    })
    .filter(Boolean);

  return c.json({ query, filters: { wing, room }, results: hits });
});

// ── GET /drawers ─────────────────────────────────────────────────
// Paginated list of all drawers — used for sync pull.
// Query params: wing?, room?, source_file?, limit?, offset?
app.get("/drawers", async (c) => {
  const wing = c.req.query("wing");
  const room = c.req.query("room");
  const source_file = c.req.query("source_file");
  const limit = Math.min(parseInt(c.req.query("limit") ?? "500"), 500);
  const offset = parseInt(c.req.query("offset") ?? "0");

  const conditions: string[] = [];
  const bindings: (string | number)[] = [];

  if (wing) {
    conditions.push("wing = ?");
    bindings.push(wing);
  }
  if (room) {
    conditions.push("room = ?");
    bindings.push(room);
  }
  if (source_file) {
    conditions.push("source_file = ?");
    bindings.push(source_file);
  }

  const where = conditions.length ? `WHERE ${conditions.join(" AND ")}` : "";

  const { results: rows } = await c.env.DB.prepare(
    `SELECT id, document, wing, room, source_file FROM drawers ${where} LIMIT ? OFFSET ?`
  )
    .bind(...bindings, limit, offset)
    .all<DrawerRow>();

  const countRow = await c.env.DB.prepare(
    `SELECT COUNT(*) as n FROM drawers ${where}`
  )
    .bind(...bindings)
    .first<{ n: number }>();

  return c.json({
    drawers: rows,
    total: countRow?.n ?? 0,
    limit,
    offset,
  });
});

// ── DELETE /drawers/:id ──────────────────────────────────────────
app.delete("/drawers/:id", async (c) => {
  const id = c.req.param("id");
  await c.env.DB.prepare("DELETE FROM drawers WHERE id = ?").bind(id).run();
  await c.env.VECTORIZE.deleteByIds([id]);
  return c.json({ ok: true, id });
});

// ── GET /status ──────────────────────────────────────────────────
// Palace stats — total drawers + wing breakdown.
app.get("/status", async (c) => {
  const countRow = await c.env.DB.prepare(
    "SELECT COUNT(*) as n FROM drawers"
  ).first<{ n: number }>();

  const { results: wings } = await c.env.DB.prepare(
    "SELECT wing, COUNT(*) as n FROM drawers GROUP BY wing ORDER BY n DESC"
  ).all<{ wing: string; n: number }>();

  return c.json({
    total_drawers: countRow?.n ?? 0,
    wings,
  });
});

export default app;
