# Etsy Agent — Design Document

Status: **accepted** (2026-09-08). Live build notes supersede anything stale here; the README and `AGENTS.md` are kept in sync with this file.

## 1. Problem

Filling out Etsy listings is tedious manual work. This project automates it: given minimal product facts (name, photos, price, category), the app drafts the listing content with an LLM, lets the user review/edit, then creates, images, and publishes the listing to Etsy through the official Etsy Open API v3. It also supports bulk CSV import and management (activate/deactivate/edit/duplicate) of existing listings.

- User faces: enter product facts + photos, review AI-drafted title/description/tags, submit.
- Personal access: a single seller managing their own shop(s). Free under Etsy's Personal Access; requires Etsy App API Key (`keystring` + `shared secret`).

## 2. Etsy Open API v3 research (verified 2026-09-08)

### 2.1 Authentication

- Base URL: `https://openapi.etsy.com/v3/application`
- **Every request** carries two headers:
  - `x-api-key: <keystring>:<shared_secret>` (colon-separated)
  - `Authorization: Bearer <access_token>` for anything shop-scoped. Listing endpoints require the OAuth token.
- Flow: **OAuth 2.0 Authorization Code + PKCE**.
  - Authorize: `https://www.etsy.com/oauth/connect?response_type=code&client_id=<keystring>&redirect_uri=<local cb>&scope=...&state=...&code_challenge=...&code_challenge_method=S256`
  - Token: POST `https://openapi.etsy.com/v3/public/oauth/token` with `grant_type=authorization_code`, `code`, `code_verifier`, `client_id`, `redirect_uri` and HTTP Basic auth `keystring:shared_secret`. Returns `access_token`, `refresh_token`, `expires_in`.
  - Refresh: same token endpoint, `grant_type=refresh_token`, `refresh_token`, `client_id`, `redirect_uri`.
- Scopes needed for this app: `listings_r`, `listings_w`, `listings_d`, `shops_r`.
- Local web app callback (`http://localhost:<port>/callback`) is ideal — Etsy allows `localhost` redirect URIs and no TLS is needed.

### 2.2 Listing lifecycle

| state | meaning | available via API |
|---|---|---|
| draft | inactive, or no image | publish (`updateListing`), delete |
| published | active, sold product listings require >0 inventory | deactivate, delete |
| deactivated | manually off-line | publish, delete |
| sold out / expired | no inventory / past end | delete, publish (expired) |

### 2.3 Creating a listing (physical product)

- `createDraftListing`: POST `/shops/{shop_id}/listings`. Mandatory: `quantity`, `title`, `description`, `price`, `who_made`, `when_made`, `taxonomy_id`, `is_supply`, and for physical items `shipping_profile_id` + `readiness_state_id` (processing profile).
- **Price is in minor units (pennies)** for `createDraftListing`/`updateListing`: `$10.99` → `1099`. **Exception: `updateListingInventory` offering `price` is the Money float (dollars, e.g. `24.99`)** — send dollars, not pennies (`build_inventory_payload` in `app/services/listing_builder.py`). See `app/etsy/money.py`.
- Images: listing cannot go live without ≥1 image. Upload with `uploadListingImage` (POST `/shops/{shop_id}/listings/{listing_id}/images`, `multipart/form-data`).
- Publish: `updateListing` (PATCH `/shops/{shop_id}/listings/{listing_id}`) with `state=active`.
- Digital products: same create, then `type=download` via `updateListing`, then `uploadListingFile`.
- Variations/inventory: `updateListingInventory` (PUT `/listings/{listing_id}/inventory`) — full cartesian products array with `property_values` (from taxonomy attributes), `offerings`, and `*_on_property` fields. Caps: 70 products (1 variation), 4900 (2), 2500 (3).

### 2.4 Taxonomy (category + attributes)

- `getSellerTaxonomyNodes` — category tree with `node_id`s (e.g., "Art & Collectibles > Prints"); feeds category dropdown. The endpoint returns a **nested tree** (roots with `children`); `_flatten_taxonomy()` in `app/main.py` explodes it into a flat `node_id` + full-path-of-names list for the dropdown.
- `getPropertiesByTaxonomyId` — per-category attributes (size/color/material/etc.) with `property_id`, `scale_id`, valid `value_ids`; feeds variation builder and extra-attribute fields.

### 2.5 Rate limits

- Per-app QPS + rolling-24h QPD (sliding window). Defaults reported in `x-limit-per-second`, `x-limit-per-day` response headers; usage in `x-remaining-today` / `x-remaining-this-secon`?.
- `429` responses include `retry-after`; client must queue + exponential backoff. Bulk work (CSV import) must throttle to stay under QPD.

### 2.6 Compliance notes

- Follow Etsy's Testing Policy: develop against drafts, use realistic test data, do not spam.
- Apps must not screen-scrape and must display the Etsy trademark disclaimer when user-facing ("This application uses the Etsy API but is not endorsed or certified by Etsy, Inc.").
- Apps dormant >6 months get banned — the local app must be used at least occasionally.

## 3. Architecture

Local **FastAPI** backend + server-served browser UI. Single Python process, SQLite persistence, thin `httpx` client (no third-party Etsy SDK — unmaintained and their OAuth doesn't fit a local callback).

```
etsyagent/
├── app/
│   ├── main.py              # FastAPI app + route wiring
│   ├── config.py            # .env loading (keystring, shared secret, LLM key, port)
│   ├── db.py                # SQLite engine / session factory
│   ├── models.py            # Product, ShopProfile, OAuthToken, SubmissionLog
│   ├── auth/oauth.py        # PKCE helper, authorize URL, token exchange + refresh
│   ├── etsy/client.py       # typed httpx client + rate-limit/throttle handling
│   ├── etsy/money.py        # minor-unit <-> dollars helpers
│   ├── ai/generator.py      # LLM adapter (OpenAI-compatible / Anthropic) draft content
│   ├── services/listing_builder.py  # product model -> Etsy calls, state tracking
│   ├── services/csv_import.py       # CSV -> Product models, throttled queue
│   ├── templates/           # Jinja2 pages
│   └── static/              # CSS
├── tests/                   # pytest
├── docs/design.md           # this file
└── pyproject.toml
```

### 3.1 Data model (SQLite)

- `OAuthToken` — single row: access/refresh token, expiry, scopes.
- `ShopProfile` — per-shop: `shop_id`, name, currency, `shipping_profile_id` (default shipping profile), `readiness_state_id` (default processing profile). Populated on connect/settings; preconditions for physical product submission.
- `Product` — the user's in-progress listing: product name, listing type (physical/download), price (dollars), quantity, who_made, when_made, is_supply, taxonomy_id + path snapshot, AI-generated + user-edited `title`, `description`, `tags` (JSON list), `materials`, photo file paths (JSON), `variations` (1–2 dimension config: property_id/name/scale_id/values) + `variants` (cartesian matrix rows: sku/price/quantity/value_labels), status (draft → queued → submitting → published/failed), `etsy_listing_id`, `error`.
- `SubmissionLog` — append-only step log per submission (audit / resumability).

### 3.2 Etsy client

- `EtsyClient.request(...)` always injects `x-api-key` and, when configured, `Authorization: Bearer`.
- Throttle: minimum spacing between calls (QPS safety), honor `retry-after` on `429` with capped exponential backoff; track rolling QPD locally and refuse bulk batches that exceed it (fail-fast instead of hammering).
- Token refresh is triggered inside the client on `401` (single-flight refresh).

### 3.3 Submission pipeline

1. Mark product `status=queued`; write `SubmissionLog`.
2. `createDraftListing` → capture `listing_id`.
3. `uploadListingImage` per photo (multipart).
4. (digital only) `uploadListingFile`, set `type=download`.
5. (variations only) `updateListingInventory`.
6. `updateListing` `state=active` → mark `published`.
7. On failure at any step: mark `failed`, log step + error; draft remains on Etsy for manual salvage; submission is idempotent (re-run resumes by existing `listing_id`).

## 4. UI (server-rendered, tabbed)

1. **Connect** — start OAuth flow; show connected shop + profile preconditions (shipping profile, processing profile) that can be completed in Settings.
2. **Products** — table of local products: name, category, price, status badge, Etsy listing id, actions (continue, submit, delete).
3. **New Listing** — multi-step form:
   - Facts: name, type, price, quantity, category (taxonomy tree, loaded + cached from Etsy), photos upload, who_made/when_made/is_supply.
   - Generate: "Generate with AI" button → fills title/description/tags (all editable); regenerate/partial fields.
   - Review: shows validated Etsy payload; "Submit to Etsy" → pipeline above.
4. **Listings** — `getListingsByShop` snapshot: title, price, state, views; actions activate/deactivate/duplicate-as-draft/delete.
5. **Import CSV** — upload, column mapping, preview, run throttled queue with progress + rate-limit budget guard.
6. **Settings** — defaults (shipping profile picker, processing profile picker, who_made/when_made defaults) + LLM provider/key.

## 5. AI content generation

- Adapter factory: provider from env (`OPENAI_API_KEY` + `OPENAI_BASE_URL` + `OPENAI_MODEL` for OpenAI-compatible — any provider, incl. free tiers like Groq; `ANTHROPIC_API_KEY` for Anthropic). `OPENAI_JSON_MODE=0` drops `response_format` for endpoints that reject it. No SDK dep — plain `httpx` against each provider's chat completion API.
- Prompt: product facts + taxonomy path + Etsy constraints (title ≤ 140, ≤ 13 tags, tag ≤ 20 chars, description in Etsy-friendly HTML paragraphs, matched who/when/is_supply context). Ask for strict JSON.
- Output validated + capped (title length, single tag length, dedupe + tag count); never exceeds Etsy limits even if the model misbehaves.

## 6. Config & secrets

- `.env` (gitignored, see `.env.example`): `ETSY_KEYSTRING`, `ETSY_SHARED_SECRET`, `ETSY_REDIRECT_PORT` (default 8000), LLM provider vars; `ETSYAGENT_DATA_DIR` (default `~/.config/etsyagent`).
- Future integration settings (roadmap): `BOOKKEEPING_DATA_DIR` (legacy CSV import),
  `GALLERY_URL` (default `http://localhost:8080`), `SQUARE_APP_ID`/`SQUARE_APP_SECRET`
  (Square OAuth2).
- Tokens and local media live under the data dir, never in the repo.

## 6.1 Running locally

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # first time
cp .env.example .env   # fill in ETSY_KEYSTRING / ETSY_SHARED_SECRET (+ optional LLM key)
.venv/bin/uvicorn app.main:app --port 8000                   # open http://localhost:8000
.venv/bin/python -m pytest                                   # tests
.venv/bin/ruff check app tests                               # lint
```

OAuth callback URL must be registered in your Etsy app as `http://localhost:8000/callback`.

## 6.2 Integrations & multi-channel sales (research, 2026-09-08)

etsyagent is one of three apps serving the same woodworking business (Six Kids
Crafts). Researching the other two + Square informed the roadmap below.

**Related apps found:**

- `~/ideal-funicular` — desktop bookkeeping (Python 3.10 + Tkinter, plain CSV via
  `data_manager.py`). Entities: `wood_inventory`, `supplies`, `products`, `expenses`,
  `sales`. The `products` schema (`product_name`, `description`, `quantity_in_stock`,
  `material_cost`, `asking_price`, `sale_price`) maps ~1:1 onto Etsy listing facts, and
  `sales` records profit/margin with stock decrement (`sales_ops.py`). Board-feet +
  weighted-average cost math in `cost_calculator.py`. Real business data lives in `data/*.csv`.
- `~/project/sixKidsCrafts` — gallery website (Spring Boot backend `scaling-octo-eureka`
  + React/Vite frontend `refactored-couscous`). Explicitly **non-ecommerce** (its own
  AGENTS.md forbids sales features), but it exposes a read-only public API
  `GET /api/gallery` → `{id, title, description, categoryId/name, published, images:
  ["/uploads/<uuid>"], mediaIds}`. Images are files in its `uploads/` dir.

**Decisions:**

- Bookkeeping **folds into etsyagent** (single unified `Product`); `ideal-funicular`
  becomes read-only legacy. One SQLite DB removes CSV syncing; etsyagent already has
  money (minor units), the web UI, and the Etsy client.
- The gallery **stays separate** and is consumed read-only via its HTTP API — no gallery
  code changes needed.
- New sales sources feed one ledger: **Etsy Shop Receipts** (`transactions_r` scope) and
  **Square Orders** (`POST /v2/orders/search`). Square money is also integer cents
  (`base_price_money.amount` = 500 = $5.00) — same minor-unit convention as Etsy.

**Target data flow:** `gallery item → Product (cost + stock + price) → listing draft →
live Etsy listing → order (Etsy/Square) → SaleRecord (revenue / profit / margin)`.

**Planned schema additions:** `Product` gains `material_cost`, `sale_price`,
`profit_margin`, `external_refs` (etsy_listing_id, square_catalog_id); new `SaleRecord`
(source = `etsy|square|manual`), `Expense`, and (optional) `SupplyPurchase`/`WoodPurchase`
tables. Cost calc + `record_sale` logic port 1:1 from `ideal-funicular` into
`app/services/bookkeeping.py` with tests.

## 6.3 Hosting: Railway (planned, 2026-09-08)

Likely deployment target for etsyagent (and, separately, the gallery). Verified from
Railway docs:

- Deployments get **ephemeral storage** — anything off a mounted volume is wiped on
  redeploy. Persistent data requires a **volume** (max **1 per service**, no replicas
  with volumes attached; sizes 0.5 GB free / 5 GB hobby / 50 GB pro).
- Autodeploy from a linked GitHub **branch**; build via Nixpacks or a Dockerfile.
  Railway injects `PORT` (bind `0.0.0.0:$PORT`).
- Public domains with TLS (`*.up.railway.app` or custom) and env vars/secrets via the
  Variables panel. Services in one project can talk over private networking
  (`<service>.railway.internal`).

Implications for this app:

1. **`ETSYAGENT_DATA_DIR` must point at the volume mount** (e.g. `/data`). The current
   default `~/.config/etsyagent` is inside the ephemeral home and would lose the SQLite
   DB + media on every deploy. SQLite health note: single-writer + one-replica volumes
   fit this single-user tool fine.
2. **OAuth redirect URIs must be the public HTTPS host** when hosted (Etsy + future
   Square). Non-`localhost` redirects require HTTPS, which Railway provides; Etsy may
   also require the remote URI be approved. Set `PUBLIC_BASE_URL` (e.g. your
   `RAILWAY_PUBLIC_DOMAIN`) and the callback host is derived from it, so the same
   pipeline works locally (default `http://localhost:<port>`) and remotely.
3. **Deploy `master`** — the release branch is what runs in production; feature work
   stays in `develop`.
4. **Gallery integration**: point `GALLERY_URL` at the gallery's public URL (or private
   network name) instead of `localhost:8080` once both are hosted.
5. The **bookkeeping fold-in** is strongly reinforced by hosting: a Tkinter desktop app
   can't be hosted at all, while a web ledger deploys alongside etsyagent.

## 7. Build phases

1. ✅ Scaffold — pyproject, config, models, db.
2. ✅ Auth + client — PKCE flow, token store, typed client, taxonomy/profile bootstrap.
3. ✅ MVP single product — facts form → AI generate → review → create draft → images → activate.
   - Category is optional at draft time (the taxonomy dropdown may be empty before
     Etsy credentials/connect); a picker on the review page sets/changes it before
     `submit_listing` (which fails fast if still missing).
4. ✅ Manage listings — table, activate/deactivate/delete.
5. 🟡 Bulk + digital + variations —
   - ✅ CSV import (column mapping, validation, error rows)
   - ✅ Digital downloads (file upload + `type=download`)
   - ✅ Variations/inventory (`updateListingInventory`) — variation-capable taxonomy
     properties drive a 1–2 dimension editor on the review page; a cartesian variant
     matrix (SKU / price / qty per combo, per-combo overrides) is submitted before
     `state=active`. Note: inventory offering `price` is the Money **float** (dollars),
     unlike `createDraftListing` minor units.
   - ⏳ Per-category extra attribute fields (non-variation) — `getPropertiesByTaxonomyId`
     also feeds these; not surfaced in the UI yet.
6. ⏳ Hardening — rate-limit tuning, resume, testing-policy-friendly dry-run.
7. ⏳ Migration bridge — importer for `ideal-funicular` `data/products.csv` + `sales.csv`
   into SQLite (`BOOKKEEPING_DATA_DIR` setting); old app keeps working untouched.
8. ⏳ Bookkeeping module — `Product` cost/stock fields, `SaleRecord` + `Expense` tables,
   `app/services/bookkeeping.py` (weighted-avg cost, sale recording), Business Dashboard
   (revenue/cost/profit/margin, per-product summaries); retires the Tkinter app.
9. ⏳ Gallery import — `GALLERY_URL` setting + "Import from gallery" page: `GET /api/gallery`
   → Product seeds (title, description, category→taxonomy hint, images via `/uploads/<stored>`).
10. ⏳ Order sync (Etsy + Square) — Etsy `transactions_r` scope (re-consent) + `getShopReceipts`/
    `getShopReceipt2`; Square OAuth2 app (`SQUARE_*` settings, `ORDERS_READ` scope);
    "Sync sales" action → idempotent `SaleRecord` rows (stock decrement, fees/tax separated).

Legend: ✅ implemented on the `feature/listing-designer` branch, 🟡 partial, ⏳ not started
(phases 7–10 are the research-backed roadmap; not yet implemented).

## 8. Decisions log

- Thin `httpx` client over an Etsy SDK (unmaintained SDKs, local-callback OAuth mismatch).
- Server-rendered Jinja2 UI, no JS framework (personal tool, minimal moving parts).
- Local DB is source of truth for in-progress work; Etsy `listing_id` back-references for published state.
- Schema drift is handled by an additive-only startup migration in `app/models.py::_ensure_schema` (`create_all` never alters existing tables), not Alembic — matches `AGENTS.md` and keeps the single-user tool dependency-free.
- Price handled as minor units at the API boundary; user enters dollars in the UI.
- LLM provider pluggable; OpenAI-compatible default, Anthropic supported.
- No Commercial Access required (single-owner personal tool).
- Bookkeeping lives **inside** etsyagent; the gallery stays separate (read-only HTTP).
- Sales ledger is multi-channel (Etsy + Square + manual), normalized on minor units;
  `SaleRecord` rows are immutable and keyed by the external order/receipt id for idempotent sync.
- Likely hosted on **Railway** (§6.3): data (SQLite + media) lives on a persistent volume
  (`ETSYAGENT_DATA_DIR=/data`), the server binds `$PORT`, and OAuth redirects use the
  public HTTPS host when deployed.