# etsyagent

I hate filling out Etsy listings — this app does it for me.

Enter minimal product facts (name, photos, price, category), the app drafts the
title/description/tags with an LLM, you review and edit, then it creates the
listing, uploads images, and publishes it to your Etsy shop through the official
Etsy Open API v3. Also supports bulk CSV import and managing existing listings.

## Quick start

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env        # fill ETSY_KEYSTRING & ETSY_SHARED_SECRET (get at
                            # https://www.etsy.com/developers/your-apps), plus an
                            # optional LLM key (OPENAI_API_KEY or ANTHROPIC_API_KEY)
.venv/bin/uvicorn app.main:app --port 8000
```

Open http://localhost:8000 and connect your shop. Register the OAuth callback
`http://localhost:8000/callback` in your Etsy app.

## Tests & lint

```sh
.venv/bin/python -m pytest
.venv/bin/ruff check app tests
```

## Design

See [docs/design.md](docs/design.md) for the API research, architecture, and build status.

The term "Etsy" is a trademark of Etsy, Inc. This application uses the Etsy API but is
not endorsed or certified by Etsy, Inc.