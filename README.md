# Syncfox

> Self-hosted, multi-cloud bidirectional sync hub. One Docker container, one web UI, your accounts.

Syncfox is a thin operator-friendly web UI on top of [`rclone bisync`](https://rclone.org/bisync/). Connect two cloud accounts (Google Drive, iCloud Drive, Dropbox), pick a folder on each, and Syncfox keeps them in lockstep — change-detection on both sides, rclone bisync as the engine, the UI tells you when an iCloud trust token is about to expire so it doesn't fail at 3 AM.

- **Web:** [syncfox.cloud](https://syncfox.cloud)
- **Image:** [`sahirmathur/syncfox`](https://hub.docker.com/r/sahirmathur/syncfox)
- **License:** MIT

---

## Why

`rclone bisync` is excellent and miserable to operate. You need to:

- write the right `rclone.conf` stanza per provider,
- remember to re-do iCloud's 2FA dance every ~30 days before the trust token dies,
- script polling so changes on either side actually reach the other side in seconds, not on the next cron tick,
- not lose credentials when you bounce the host.

Syncfox is the boring operator UI that handles those four things. The actual sync is still `rclone bisync` under the hood — fork it, replace it, audit it.

## Features

- **Three providers out of the box** — Google Drive (OAuth or service-account), iCloud Drive (Apple ID + 2FA via rclone's non-interactive state machine), Dropbox (PKCE OAuth).
- **Per-pair watchers** — Drive `changes.list` watermark + iCloud root-folder fingerprint. Edits propagate in ~30 seconds, not on the next cron run.
- **iCloud trust-token expiry surface** — every iCloud remote on `/remotes` shows a green/orange/red badge for days remaining. Click "Re-authenticate" → enter 2FA code → done. Optional daily Discord nudge ≤25d before expiry.
- **Onboarding wizard** — `/setup` walks first-run users through provider 1 → provider 2 → first pair, then steps aside.
- **Opt-in encryption at rest** — set `SYNCFOX_MASTER_PASSWORD` and credentials in SQLite are Fernet-encrypted (PBKDF2-SHA256, 200k rounds). Off by default so existing installs don't break.
- **Multi-arch image** — `linux/amd64` + `linux/arm64`. Runs on a Pi 4/5, Apple Silicon, x86 server.
- **No telemetry, no phone-home, no signup.** Your container, your data, your network.

## Quickstart

```bash
mkdir syncfox && cd syncfox
curl -O https://raw.githubusercontent.com/sahirmathur1/syncfox/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/sahirmathur1/syncfox/main/.env.example
# edit .env — only PUBLIC_BASE_URL is required for a local-only setup
docker compose up -d
open http://localhost:8081
```

The container will:

1. Initialise SQLite + run migrations under `./data/cloudsync.db`.
2. Start the web UI on port `8081`.
3. Redirect you to `/setup` until you've connected two accounts and made one pair.

## Provider matrix

| Provider | Auth | Token lifetime | Re-auth needed? |
|---|---|---|---|
| Google Drive | OAuth (browser) or service-account JSON | refresh tokens don't expire | rare (only on revoke) |
| iCloud Drive | Apple ID + 2FA (rclone state machine) | trust token ≈30 days | yes — UI shows badge + "Re-authenticate" button |
| Dropbox | PKCE OAuth (browser, no server-side secret needed) | refresh tokens don't expire | rare (only on revoke) |

## Configuration

Everything is environment variables — copy [`.env.example`](.env.example) to `.env` and edit. The minimum is:

```env
PUBLIC_BASE_URL=http://localhost:8081
```

Set `PUBLIC_BASE_URL` to whatever URL your reverse-proxy fronts (e.g. `https://sync.example.com`) — Syncfox uses it to construct OAuth callback URLs. Provider client IDs/secrets only need to be set for providers you actually use.

### Optional: encryption at rest

```env
SYNCFOX_MASTER_PASSWORD=<a long random string you back up safely>
```

When set, all newly-stored credential blobs are encrypted with Fernet. Existing plaintext blobs continue to decrypt fine (back-compat). **There is no recovery if you lose this password** — re-add the affected providers from scratch. Document it in your password manager *before* you set it.

### Optional: daily Discord nudge for expiring iCloud tokens

```env
SYNCFOX_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
SYNCFOX_NUDGE_DAYS=25
SYNCFOX_NUDGE_LOCAL_TZ=America/Edmonton
SYNCFOX_NUDGE_LOCAL_HOUR=9
```

Posts once a day at the configured local hour for any iCloud remote whose trust token is within `SYNCFOX_NUDGE_DAYS` of expiry. No-op if `SYNCFOX_DISCORD_WEBHOOK_URL` is unset.

### Reverse-proxy + auth

Syncfox does **not** enforce authentication on its own — it expects a reverse-proxy in front. Example Caddyfile:

```caddy
sync.example.com {
    basicauth {
        operator <bcrypt-hash-from-`caddy hash-password`>
    }
    reverse_proxy 127.0.0.1:8081
}
```

For a fully local-only setup (`PUBLIC_BASE_URL=http://localhost:8081`), bind to `127.0.0.1` and you're done.

## Architecture

```
┌──────────────────────────────────────────────┐
│ Syncfox container (single image, multi-arch) │
│                                              │
│  FastAPI + uvicorn  ──►  server-rendered HTML│
│       │                                      │
│       ├──► SQLite (WAL)  /data/cloudsync.db  │
│       │     • remotes (encrypted creds)      │
│       │     • pairs                          │
│       │     • run history                    │
│       │                                      │
│       ├──► rclone subprocess  ◄──┐           │
│       │     • bisync (engine)    │           │
│       │     • config rebuilt     │           │
│       │       from DB on change  │           │
│       │                          │           │
│       └──► async tasks           │           │
│            • per-pair watcher   ─┘           │
│              (Drive watermark / iCloud       │
│              fingerprint polling, ~30s)      │
│            • daily expiry nudge cron         │
└──────────────────────────────────────────────┘
                    │
                    ▼ (operator's reverse-proxy)
              https://sync.example.com
```

- **Stack:** Python 3.12 / FastAPI / uvicorn / SQLite (WAL) / `rclone bisync` / `cryptography` for opt-in Fernet encryption.
- **State:** all in `/data` (bind-mount it; that's your backup target).
- **No JS framework** — server-rendered HTML, no build step, view-source friendly.
- **Tests:** `pytest` under `tests/unit/`. Run `uv run pytest -q`.

## Repo layout

```
src/cloud_sync/
  main.py                FastAPI app + lifespan + middleware
  config.py              pydantic-settings (.env-driven)
  persistence/
    db.py                SQLite open + migrations
    repos.py             remotes / pairs / runs CRUD
  providers/
    google.py            Drive OAuth + service-account
    icloud.py            Apple ID + 2FA state machine
    dropbox.py           PKCE OAuth
    expiry.py            days_remaining + badge colour
  routes/
    setup.py             /setup wizard
    remotes.py           list + per-row badge + re-auth button
    google_setup.py      Google OAuth handlers
    icloud_setup.py      iCloud 2FA + re-auth
    dropbox_setup.py     Dropbox PKCE handlers
    pairs.py             pair CRUD + per-pair detail
    health.py            /healthz
  sync/
    rclone.py            subprocess wrapper + conf-stanza writers
    conf_writer.py       full rclone.conf rebuild from DB
    bisync.py            bisync runner + run-row persistence
    encryption.py        opt-in Fernet (PBKDF2-SHA256, 200k)
    nudges.py            daily expiry-nudge cron
    vaultwarden.py       optional Bitwarden CLI integration
  static/                HTML templates (server-rendered)
migrations/              .sql files, applied in order at startup
tests/unit/              pytest suite
```

## Development

```bash
uv sync --extra dev
uv run pytest -q
uv run uvicorn cloud_sync.main:app --reload --port 8081
```

The `cloud_sync` import name is preserved (vs the project name `syncfox`) for backwards-compat with installs that pre-date the rename.

## Contributing

Bug reports + small focused PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the triage policy (best-effort, no SLA — this is a side project) and what kind of changes are likely to land vs not.

## License

MIT — see [LICENSE](LICENSE).

## Author

[@sahirmathur1](https://github.com/sahirmathur1) — built because I needed to keep Obsidian vaults in sync between iCloud Drive (where I edit on iPhone) and Google Drive (where the rest of my notes live), and every existing tool either needed a paid SaaS account or made me re-do iCloud 2FA every month from a terminal. If Syncfox saves you a Saturday afternoon of YAML-wrangling, that's the goal.
