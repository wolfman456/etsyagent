# AGENTS.md

## Project

Local web app that drafts and publishes Etsy listings via the Etsy Open API v3.
FastAPI (async) backend, server-rendered Jinja2 UI, SQLite (SQLAlchemy 2.x), thin
`httpx` client — **no third-party Etsy SDK**. An LLM (OpenAI-compatible or Anthropic)
drafts listing copy. See `docs/design.md` for architecture and build status.

etsyagent is one of three apps for the same woodworking business. Research-backed
roadmap (docs/design.md §6.2): bookkeeping (`~/ideal-funicular`, Tkinter + CSV) will
fold into this app; the gallery (`~/project/sixKidsCrafts`, Spring Boot + React) stays
separate and is consumed read-only via its public `GET /api/gallery`; future sales sync
comes from Etsy Shop Receipts (`transactions_r`) and Square Orders (`POST /v2/orders/search`,
integer-cents money). Nothing beyond the docs has changed yet — phases 7–10 are ⏳.

## Commands

```sh
.venv/bin/python -m pytest          # all tests
.venv/bin/ruff check app tests      # lint
.venv/bin/uvicorn app.main:app --port 8000   # local app
```

- Python 3.10 target (`pyproject.toml` pins `requires-python >= 3.10`). No
  system pip/ensurepip on this machine: the venv was bootstrapped by copying the
  `pip` package in from another project's venv — use `.venv/bin/python -m pip`.
- Project installed editable: `pip install -e ".[dev]"`; new deps go in
  `[project.dependencies]` (runtime) / `[project.optional-dependencies].dev`.

## Structure

- `app/main.py` — FastAPI app + all routes; `template()` helper injects
  `shop`, `settings`, connection state into every page. Routes are `async`.
- `app/models.py` — SQLAlchemy ORM. `Product` is the local source of truth for an
  in-progress/draft listing; `SubmissionLog` records pipeline steps.
- `app/services/bookkeeping.py` — **planned** (phase 8): sale recording with stock
  decrement + profit/margin, weighted-average cost (ported from `ideal-funicular`).
- `app/etsy/client.py` — `EtsyClient` (QPS + rolling-24h QPD guard, 429 backoff,
  401 single-flight refresh). **Every Etsy call needs both `x-api-key:
  keystring:secret` and `Bearer` token.**
- `app/etsy/money.py` — UI works in dollars, Etsy API in **minor units** (pennies).
  Square's money objects are integer cents too — same convention.
- `app/auth/oauth.py` — OAuth2 Authorization Code + PKCE; token endpoint uses
  HTTP Basic (`keystring:secret`). Etsy access tokens are short-lived; the client
  refreshes automatically on 401. Square OAuth2 (planned, phase 10) reuses this pattern.
- `app/services/listing_builder.py` — create draft → upload images/file →
  `state=active`; validates required fields and shop profile presence first.
- `app/services/csv_import.py` — CSV → `Product` rows (column aliases, validation).
- `app/ai/generator.py` — LLM adapter producing strict-JSON `{title, description,
  tags, materials}`; output is always capped to Etsy limits (140-char title,
  ≤13 tags of ≤20 chars).

## Gotchas

- **From `models.py`: session rows passed to templates must be detached-safe.**
  The `_warm()` helper in `app/main.py` force-loads column attributes before the
  `SessionLocal` context closes; always `_warm()` objects handed to a template.
- **Schema drift is handled at startup, not by Alembic.** `create_all` never alters
  an existing table, so an old DB missing a new ORM column 500s every page until
  `_ensure_schema()` in `app/models.py` adds it (`ALTER TABLE ... ADD COLUMN`, additive
  only — never drops/retypes). It runs on every boot, so just restarting the app
  repairs a stale DB.
- SQLite on-disk file lives at `~/.config/etsyagent/etsyagent.db` (or
  `ETSYAGENT_DATA_DIR`); media uploads land in `{data_dir}/media`. Never commit
  tokens/media — `.gitignore` only ignores `.env` and `.data/`.
- OAuth flow: `/connect/start` stores the PKCE verifier in process-memory
  `_pending_states` keyed by state (single-user tool, resets on restart).
- Taxonomy list is fetched live from Etsy and cached in-memory for 4h (public
  endpoint, token not required); the category dropdown relies on it.
- `createDraftListing` requires `shipping_profile_id` + `readiness_state_id` for
  **physical** listings — must be picked in Settings after connect; submission
  fails fast with a clear message otherwise. Digital listings skip both.
- Price in `createDraftListing`/`updateListing` is minor units (`$10.99` → `1099`) —
  never send dollars straight through. **Exception: `updateListingInventory` offering
  `price` is the Money float (`24.99`)**, handled by `build_inventory_payload`.
- When running tests, `SessionLocal`/`engine` in `app/models.py` bind to the real
  data dir; tests use their own in-memory engine (`tests/test_listing_builder.py`).
- In `etsyagent` the Etsy trademark disclaimer is displayed on the Connect page
  (required by Etsy API terms).
- **Railway hosting (docs/design.md §6.3, live)**: deployments have ephemeral
  filesystems — set `ETSYAGENT_DATA_DIR` to a mounted volume (e.g. `/data`), bind
  `0.0.0.0:$PORT`, and set `PUBLIC_BASE_URL` (public HTTPS host) so the OAuth
  callback uses the remote host instead of `localhost`. Deploy from `master` via the
  checked-in `Dockerfile` (builder = docker in `railway.toml`); use `railway deployment up`
  for manual deploys — `.railwayignore` stops secrets/venv being uploaded.

## Workflow

- Default branch: `develop`, where all work lands. `master` only ever moves by
  merging `develop` into it (release branch). Both branches are protected on
  GitHub: required PR + 1 approving review, no force-push, no deletions, no
  direct pushes (enforced on admins too).
- Never push to `develop` or `master` directly. Work on `feature/<slug>` topic
  branches off `develop`; the maintainer reviews/merges PRs into `develop`.
- Once work is in `develop` and ready to release, open a PR `develop` → `master`
  and let the maintainer merge it (no other changes go to `master`).
- Commit messages read like changelog entries: concise summary line + body of key
  changes and reasoning. Reference `docs/design.md` sections when a change
  implements one.
- Docs are part of the deliverable: when a change ships documented behavior,
  update `docs/design.md` status + README in the same change.
- Start work with `git fetch origin && git switch -c feature/<slug> origin/develop`.