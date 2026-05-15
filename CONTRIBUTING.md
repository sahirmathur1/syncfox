# Contributing to Syncfox

Thanks for taking the time. Two things up front:

1. **Syncfox is a side project, maintained best-effort.** No SLA on issue triage, PR review, or releases. If something is urgent for you and not for me, the right move is to fork — that's what MIT is for.
2. **The smaller and more focused the change, the more likely it lands.** A 30-line PR that does one thing gets reviewed in a sitting. A 1,500-line refactor probably won't.

## What's likely to land

- Bug fixes with a reproducer (steps to reproduce + what you expected vs got).
- Provider improvements that don't expand the surface area dramatically (e.g. handling a new Drive error code, fixing iCloud 2FA edge cases).
- Doc fixes — typos, clarifications, missing env vars, broken links.
- Test coverage for paths that don't have any.
- Performance fixes with a before/after measurement.

## What probably won't

- New providers without a real maintenance commitment from the contributor. Each provider is a 30-day pager rotation: 2FA changes, OAuth scope shifts, API deprecations. If I can't reach you when Box rotates an OAuth scope in 18 months, I'll have to remove the integration. Open an issue first to discuss.
- Rewrites in another language / framework / database. Syncfox is intentionally Python+SQLite+rclone — that's the whole product.
- Adding telemetry, analytics, "anonymous usage stats", or any phone-home behaviour. Hard no.
- Adding a paid tier, subscription, or licence-key gate. Also hard no — MIT means MIT.
- Big UI redesigns. The current UI is intentionally drab; "looks slick" isn't a goal.

## Workflow

1. Open an issue describing the bug or proposing the change *before* writing the code, if it's more than ~50 lines. Saves both of us from a "this isn't quite the direction I want to go" review thread.
2. Fork, branch off `main`, make the change.
3. Run the tests: `uv run pytest -q`.
4. Open a PR with: what changed, why, how you tested.
5. CI must be green before review.

## Local dev

```bash
git clone https://github.com/sahirmathur1/syncfox.git
cd syncfox
uv sync --extra dev
uv run pytest -q
uv run uvicorn cloud_sync.main:app --reload --port 8081
```

The `cloud_sync` import name is preserved (project name is `syncfox`); don't rename the import path in a PR — too much downstream blast radius for the cosmetic win.

## Security

If you find a security issue, **please don't open a public issue.** Email it to the address in [my GitHub profile](https://github.com/sahirmathur1) with "syncfox security" in the subject, or DM me on whatever platform we're already mutuals on. I'll respond within a week and credit you in the fix unless you'd rather stay anonymous.

## Code of conduct

Be kind. Assume the other person is doing their best with the time they have. That's it.
