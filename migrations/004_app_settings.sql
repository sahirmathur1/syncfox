-- Per-install app config (key/value).
-- Phase 5 follow-up: Dropbox OAuth app credentials enter via the UI
-- instead of requiring an .env edit + restart. Other providers may
-- follow the same pattern later.

create table if not exists app_settings (
  key         text primary key,
  value       text not null,
  updated_at  text not null default (datetime('now'))
);
