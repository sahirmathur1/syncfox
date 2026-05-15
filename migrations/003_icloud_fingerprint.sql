-- Add iCloud fingerprint per pair.
-- Hash of `rclone lsf -R --format=tsp` output for the iCloud side; if it
-- changes between watcher ticks, something on iCloud changed → trigger a
-- bisync. Lets us drop the 5-min idle-floor and react in ~30s.
alter table pairs add column icloud_fingerprint text;
