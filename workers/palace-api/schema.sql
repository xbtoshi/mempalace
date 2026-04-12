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
