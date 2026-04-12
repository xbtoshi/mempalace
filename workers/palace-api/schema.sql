CREATE TABLE IF NOT EXISTS drawers (
  id           TEXT PRIMARY KEY,
  document     TEXT NOT NULL,
  wing         TEXT NOT NULL DEFAULT 'default',
  room         TEXT NOT NULL DEFAULT 'general',
  source_file  TEXT NOT NULL DEFAULT '',
  created_at   INTEGER NOT NULL DEFAULT (unixepoch()),
  updated_at   INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_drawers_wing      ON drawers(wing);
CREATE INDEX IF NOT EXISTS idx_drawers_room      ON drawers(room);
CREATE INDEX IF NOT EXISTS idx_drawers_wing_room ON drawers(wing, room);
CREATE INDEX IF NOT EXISTS idx_drawers_source    ON drawers(source_file);

-- Knowledge Graph tables
CREATE TABLE IF NOT EXISTS kg_entities (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  type       TEXT DEFAULT 'unknown',
  properties TEXT DEFAULT '{}',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS kg_triples (
  id             TEXT PRIMARY KEY,
  subject        TEXT NOT NULL,
  predicate      TEXT NOT NULL,
  object         TEXT NOT NULL,
  valid_from     TEXT,
  valid_to       TEXT,
  confidence     REAL DEFAULT 1.0,
  source_closet  TEXT,
  source_file    TEXT,
  extracted_at   TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (subject) REFERENCES kg_entities(id),
  FOREIGN KEY (object) REFERENCES kg_entities(id)
);

CREATE INDEX IF NOT EXISTS idx_kg_triples_subject   ON kg_triples(subject);
CREATE INDEX IF NOT EXISTS idx_kg_triples_object    ON kg_triples(object);
CREATE INDEX IF NOT EXISTS idx_kg_triples_predicate ON kg_triples(predicate);
CREATE INDEX IF NOT EXISTS idx_kg_triples_valid     ON kg_triples(valid_from, valid_to);
