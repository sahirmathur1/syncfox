-- cloud-sync initial schema.

create table if not exists rclone_remotes (
  name              text primary key,
  provider          text not null check (provider in ('google','icloud','dropbox')),
  account_label     text,
  encrypted_config  text not null,
  created_at        text not null default (datetime('now')),
  last_verified_at  text
);

create table if not exists pairs (
  id                  text primary key,
  name                text unique not null,
  source_remote       text not null references rclone_remotes(name),
  source_path         text not null,
  destination_remote  text not null references rclone_remotes(name),
  destination_path    text not null,
  poll_seconds        integer not null default 30,
  filters             text not null default '',
  conflict_resolve    text not null default 'newer' check (conflict_resolve in ('newer','older','larger','smaller')),
  paused              integer not null default 0,
  initial_resync_done integer not null default 0,
  last_success_at     text,
  last_failure_at     text,
  created_at          text not null default (datetime('now'))
);

create table if not exists pair_runs (
  id              text primary key,
  pair_id         text not null references pairs(id) on delete cascade,
  started_at      text not null,
  ended_at        text,
  trigger         text not null check (trigger in ('poll-detected','manual','resync','startup')),
  exit_code       integer,
  status          text check (status in ('running','ok','fail','skipped')),
  files_added     integer,
  files_deleted   integer,
  files_changed   integer,
  conflicts       integer,
  log_path        text,
  error_summary   text
);
create index if not exists pair_runs_pair_started on pair_runs(pair_id, started_at desc);
