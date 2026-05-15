-- Add Drive change-API watermark per pair.
-- Track the last `startPageToken` we got from drive.changes.list so we
-- can ask "anything since then?" on every poll cycle without re-listing.
alter table pairs add column drive_changes_token text;

-- Track the last time we ran a full bisync regardless of watermark, so
-- we can floor iCloud-side change latency at e.g. 5 min until we ship (2).
alter table pairs add column last_full_bisync_at text;
