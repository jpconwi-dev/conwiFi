-- Migration 3 (additive): session pause + admin-managed dashboard images.
ALTER TABLE sessions ADD COLUMN paused_at INTEGER;
ALTER TABLE sessions ADD COLUMN paused_remaining INTEGER;

CREATE TABLE IF NOT EXISTS media (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'image' CHECK (kind IN ('image')),
  file TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  link TEXT NOT NULL DEFAULT '',
  show_on TEXT NOT NULL DEFAULT 'both' CHECK (show_on IN ('both','portal','dashboard')),
  enabled INTEGER NOT NULL DEFAULT 1,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_media_order ON media(enabled, sort_order, id);
