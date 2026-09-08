from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.ai.generator import ProductFacts, generate_draft
from app.auth.oauth import (
    build_authorize_url,
    build_oauth_config,
    exchange_code,
    generate_pkce_pair,
    new_state,
)
from app.config import settings
from app.etsy.client import EtsyClient, EtsyError, SqliteTokenStore
from app.etsy.money import parse_price
from app.models import (
    Product,
    SessionLocal,
    ShopProfile,
)
from app.services.csv_import import parse_csv
from app.services.listing_builder import (
    WHEN_MADE_CHOICES,
    WHO_MADE_CHOICES,
    ListingValidationError,
    submit_listing,
)

SCOPES = ["listings_r", "listings_w", "listings_d", "shops_r"]

app = FastAPI(title="Etsy Agent")
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/media", StaticFiles(directory=str(settings.media_dir)), name="media")

token_store = SqliteTokenStore(SessionLocal)
# OAuth pending states: state -> {verifier, created_at}
_pending_states: dict[str, dict] = {}
# Lightweight taxonomy cache: node_id -> node; refreshed on demand with TTL.
_taxonomy_cache: dict = {"fetched_at": 0.0, "nodes": []}


def get_client() -> EtsyClient:
    return EtsyClient(settings, token_store)


def get_shop_profile(session: Session) -> ShopProfile | None:
    return session.query(ShopProfile).order_by(ShopProfile.shop_id).first()


def _warm(obj):
    """Force-load all column attributes so the instance survives session close."""
    if obj is None:
        return None
    for column in obj.__mapper__.column_attrs:
        getattr(obj, column.key)
    return obj


def client_with_shop(request: Request) -> tuple[EtsyClient, ShopProfile | None]:
    with SessionLocal() as session:
        shop = _warm(get_shop_profile(session))
    request.state.shop = shop
    return get_client(), shop


def template(
    request: Request, name: str, context: dict | None = None, *, status_code: int = 200
) -> Response:
    with SessionLocal() as session:
        shop = _warm(get_shop_profile(session))
    context = context or {}
    context["settings"] = settings
    context["shop"] = shop
    context["connected"] = bool(token_store.load())
    context["scopes_ok"] = _scopes_ok()
    context["creds_ok"] = settings.etsy_credentials_set
    context["who_made_choices"] = WHO_MADE_CHOICES
    context["when_made_choices"] = WHEN_MADE_CHOICES
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def _scopes_ok() -> bool:
    token = token_store.load()
    if not token:
        return False
    wanted = set(SCOPES)
    have = set(token.get("scopes", "").split())
    return wanted.issubset(have)


async def _ensure_taxonomy() -> list[dict]:
    if time.time() - _taxonomy_cache["fetched_at"] < 4 * 3600 and _taxonomy_cache["nodes"]:
        return _taxonomy_cache["nodes"]
    client = get_client()
    nodes = await client.get_seller_taxonomy() if _has_client() else []
    if nodes:
        _taxonomy_cache["fetched_at"] = time.time()
        _taxonomy_cache["nodes"] = nodes
    return nodes


def _has_client() -> bool:
    return settings.etsy_credentials_set


def taxonomy_options(nodes: list[dict]) -> list[tuple[int, str]]:
    options: list[tuple[int, str]] = []
    for node in nodes:
        paths = node.get("full_path_taxonomy_paths") or []
        label_parts = paths[0] if paths else [node.get("name", "")]
        label = " > ".join(label_parts)
        options.append((node["node_id"], label))
    return sorted(options, key=lambda item: item[1].lower())


# ---------------------------------------------------------------------------
# Connect / OAuth
# ---------------------------------------------------------------------------

@app.get("/connect", response_class=HTMLResponse)
def connect_page(request: Request):
    return template(request, "connect.html")


@app.get("/connect/start")
def connect_start(request: Request):
    if not settings.etsy_credentials_set:
        raise HTTPException(status_code=400, detail="Etsy credentials not configured (.env)")
    oauth_cfg = build_oauth_config(settings, SCOPES)
    state = new_state()
    verifier, challenge = generate_pkce_pair()
    _pending_states[state] = {"verifier": verifier, "created_at": time.time()}
    url = build_authorize_url(oauth_cfg, state, challenge)
    return RedirectResponse(url, status_code=303)


@app.get("/callback", response_class=HTMLResponse)
async def oauth_callback(request: Request, code: str | None = None, state: str | None = None):
    error = None
    if not code or not state:
        error = "Callback missing code or state."
    else:
        pending = _pending_states.pop(state, None)
        if not pending:
            error = "Unknown or expired OAuth state (start the connect flow again)."
        else:
            try:
                oauth_cfg = build_oauth_config(settings, SCOPES)
                async with __import__("httpx").AsyncClient() as client:
                    body = await exchange_code(oauth_cfg, code, pending["verifier"], client)
                token = {
                    "access_token": body["access_token"],
                    "refresh_token": body.get("refresh_token", ""),
                    "scopes": " ".join(SCOPES),
                }
                token_store.save(token)
                await _bootstrap_shop()
                return RedirectResponse("/?connected=1", status_code=303)
            except Exception as exc:  # noqa: BLE001
                error = f"Token exchange failed: {exc}"
    return template(request, "connect.html", {"oauth_error": error})


async def _bootstrap_shop() -> None:
    client = get_client()
    shops = await client.get_user_shops()
    if not shops:
        return
    shop_data = shops[0]
    shop_id = shop_data["shop_id"]
    shipping = await client.get_shop_shipping_profiles(shop_id)
    processing = await client.get_shop_processing_profiles(shop_id)
    sections = await client.get_shop_sections(shop_id)
    currency = "USD"
    try:
        currency = (await client.get_shop(shop_id)).get("currency_code", "USD")
    except EtsyError:
        pass
    with SessionLocal() as session:
        profile = get_shop_profile(session)
        if profile is None:
            profile = ShopProfile(shop_id=shop_id)
        else:
            profile.shop_id = shop_id
        profile.shop_name = shop_data.get("shop_name", "")
        profile.currency_code = currency
        profile.shipping_profiles_json = shipping
        profile.processing_profiles_json = processing
        profile.shop_sections_json = sections
        if shipping:
            profile.shipping_profile_id = (
                profile.shipping_profile_id or shipping[0]["shipping_profile_id"]
            )
        if processing:
            profile.readiness_state_id = (
                profile.readiness_state_id or processing[0]["processing_profile_id"]
            )
        session.add(profile)
        session.commit()


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return RedirectResponse("/products", status_code=303)


@app.get("/products", response_class=HTMLResponse)
def products_page(request: Request):
    with SessionLocal() as session:
        products = (
            session.query(Product).order_by(Product.updated_at.desc()).all()
        )
    return template(request, "products.html", {"products": [p.to_summary() for p in products]})


@app.get("/products/new", response_class=HTMLResponse)
async def product_new_page(request: Request):
    with SessionLocal() as session:
        shop = _warm(get_shop_profile(session))
    nodes = await _ensure_taxonomy()
    options = taxonomy_options(nodes) if nodes else []
    return template(
        request,
        "product_form.html",
        {
            "product": None,
            "taxonomy_options": options,
            "default_who_made": shop.default_who_made if shop else "i_did",
            "default_when_made": shop.default_when_made if shop else "made_to_order",
            "default_is_supply": shop.default_is_supply if shop else False,
        },
    )


@app.post("/products/new", response_class=HTMLResponse)
async def product_new_submit(
    request: Request,
    name: str = Form(...),
    price: str = Form(...),
    quantity: int = Form(1),
    listing_type: str = Form("physical"),
    taxonomy_id: int = Form(...),
    taxonomy_path: str = Form(""),
    who_made: str = Form("i_did"),
    when_made: str = Form("made_to_order"),
    is_supply: int = Form(0),
    notes: str = Form(""),
    images: Annotated[list[UploadFile], File()] = None,
    digital_file: Annotated[UploadFile | None, File()] = None,
):
    try:
        parse_price(price)
    except ValueError:
        return template(request, "product_form.html", {"error": "Invalid price", "product": None})
    with SessionLocal() as session:
        product = Product(
            name=name.strip(),
            price=price.strip(),
            quantity=max(quantity, 1),
            listing_type="download" if listing_type == "download" else "physical",
            taxonomy_id=taxonomy_id,
            taxonomy_path=taxonomy_path or f"taxonomy {taxonomy_id}",
            who_made=who_made if who_made in WHO_MADE_CHOICES else "i_did",
            when_made=when_made if when_made in WHEN_MADE_CHOICES else "made_to_order",
            is_supply=bool(is_supply),
        )
        shop = get_shop_profile(session)
        product.shop_id = shop.shop_id if shop else None
        saved_images: list[str] = []
        for img in images or []:
            if not img.filename:
                continue
            target = settings.media_dir / img.filename
            target.write_bytes(await img.read())
            saved_images.append(target.name)
        product.images = saved_images
        if digital_file and digital_file.filename:
            target = settings.media_dir / digital_file.filename
            target.write_bytes(await digital_file.read())
            product.digital_file = target.name
        session.add(product)
        session.commit()
        product_id = product.id
    return RedirectResponse(f"/products/{product_id}?note={notes}", status_code=303)


@app.get("/products/{product_id}", response_class=HTMLResponse)
def product_detail(request: Request, product_id: int):
    with SessionLocal() as session:
        product = session.get(Product, product_id)
        if not product:
            raise HTTPException(status_code=404, detail="Product not found")
        logs = [{"step": log.step, "detail": log.detail} for log in product.logs]
        product_snapshot = _warm(product)
        shop = _warm(get_shop_profile(session))
    return template(
        request,
        "product_review.html",
        {
            "product": product_snapshot,
            "logs": logs,
            "shop": shop,
        },
    )


@app.post("/products/{product_id}/generate", response_class=HTMLResponse)
async def product_generate(request: Request, product_id: int):
    """Call the LLM and fill title/description/tags from the current facts."""
    with SessionLocal() as session:
        product = session.get(Product, product_id)
        if not product:
            raise HTTPException(status_code=404, detail="Product not found")
        facts = ProductFacts(
            name=product.name,
            taxonomy_path=product.taxonomy_path,
            listing_type=product.listing_type,
            price=product.price,
            who_made=product.who_made,
            when_made=product.when_made,
            is_supply=product.is_supply,
            notes=request.query_params.get("note", ""),
        )
        try:
            draft = await generate_draft(settings, facts)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"AI generation failed: {exc}") from exc
        product.title = draft.title
        product.description = draft.description
        product.tags = draft.tags
        product.materials = draft.materials
        session.add(product)
        session.commit()
        product_snapshot = _warm(product)
        logs = [{"step": log.step, "detail": log.detail} for log in product.logs]
        shop = _warm(get_shop_profile(session))
    return template(
        request,
        "product_review.html",
        {"product": product_snapshot, "logs": logs, "shop": shop, "generated": True},
    )


@app.post("/products/{product_id}/save", response_class=HTMLResponse)
def product_save(
    request: Request,
    product_id: int,
    title: str = Form(...),
    description: str = Form(...),
    tags: str = Form(""),
    materials: str = Form(""),
):
    with SessionLocal() as session:
        product = session.get(Product, product_id)
        if not product:
            raise HTTPException(status_code=404, detail="Product not found")
        product.title = title
        product.description = description
        product.tags = [t.strip() for t in tags.split(",") if t.strip()][:13]
        product.materials = [m.strip() for m in materials.split(",") if m.strip()][:13]
        session.add(product)
        session.commit()
        product_id = product.id
    return RedirectResponse(f"/products/{product_id}", status_code=303)


@app.post("/products/{product_id}/submit")
async def product_submit(request: Request, product_id: int):
    with SessionLocal() as session:
        product = session.get(Product, product_id)
        if not product:
            raise HTTPException(status_code=404, detail="Product not found")
        shop = get_shop_profile(session)
        if shop is None:
            raise HTTPException(status_code=400, detail="Connect your shop first.")
        try:
            await submit_listing(product, shop, get_client(), session, settings.media_dir)
        except ListingValidationError as exc:
            session.rollback()
            return RedirectResponse(f"/products/{product_id}?error={str(exc)}", status_code=303)
        except (EtsyError, FileNotFoundError) as exc:
            product.status = "failed"
            product.error = str(exc)
            session.add(product)
            session.commit()
            return RedirectResponse(f"/products/{product_id}?error={str(exc)}", status_code=303)
        product_id = product.id
    return RedirectResponse("/products?submitted=1", status_code=303)


@app.post("/products/{product_id}/delete")
def product_delete(request: Request, product_id: int):
    with SessionLocal() as session:
        product = session.get(Product, product_id)
        if product:
            session.delete(product)
            session.commit()
    return RedirectResponse("/products", status_code=303)


# ---------------------------------------------------------------------------
# Remote listings
# ---------------------------------------------------------------------------

@app.get("/listings", response_class=HTMLResponse)
async def listings_page(request: Request, state: str | None = None):
    client, shop = client_with_shop(request)
    if not shop:
        return template(request, "listings.html", {"listings": [], "state": state})
    data = await client.get_listings_by_shop(shop.shop_id, state=state, limit=100)
    listings = data.get("results", [])
    return template(request, "listings.html", {"listings": listings, "state": state})


@app.post("/listings/{listing_id}/{action}")
async def listing_action(request: Request, listing_id: int, action: str):
    client, shop = client_with_shop(request)
    if not shop or not _has_client():
        raise HTTPException(status_code=400, detail="Shop not connected.")
    if action in {"activate", "deactivate"}:
        params = {"state": "active" if action == "activate" else "inactive"}
        await client.update_listing(shop.shop_id, listing_id, **params)
    elif action == "delete":
        await client.delete_listing(listing_id)
    else:
        raise HTTPException(status_code=404)
    return RedirectResponse("/listings", status_code=303)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    return template(request, "import.html")


@app.post("/import", response_class=HTMLResponse)
def import_submit(request: Request, file: Annotated[UploadFile, File()]):
    content = file.file.read().decode("utf-8-sig", errors="replace")
    result = parse_csv(content)
    shop = None
    with SessionLocal() as session:
        shop = get_shop_profile(session)
        for product in result.products:
            product.shop_id = shop.shop_id if shop else None
            session.add(product)
        if result.products:
            session.commit()
    return template(
        request,
        "import.html",
        {
            "imported": len(result.products),
            "errors": [{"row": e.row, "message": e.message} for e in result.errors],
        },
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    _client, shop = client_with_shop(request)
    shipping = shop.shipping_profiles_json if shop else []
    processing = shop.processing_profiles_json if shop else []
    return template(
        request,
        "settings.html",
        {
            "shipping_profiles": shipping,
            "processing_profiles": processing,
            "token": token_store.load(),
        },
    )


@app.post("/settings/refresh")
async def settings_refresh(request: Request):
    await _bootstrap_shop()
    return RedirectResponse("/settings?refreshed=1", status_code=303)


@app.post("/settings/save")
def settings_save(
    request: Request,
    shipping_profile_id: int = Form(0),
    processing_profile_id: int = Form(0),
    default_who_made: str = Form("i_did"),
    default_when_made: str = Form("made_to_order"),
    default_is_supply: int = Form(0),
):
    with SessionLocal() as session:
        shop = get_shop_profile(session)
        if shop is None:
            raise HTTPException(status_code=400, detail="Connect your shop first.")
        if shipping_profile_id:
            shop.shipping_profile_id = shipping_profile_id
        if processing_profile_id:
            shop.readiness_state_id = processing_profile_id
        shop.default_who_made = (
            default_who_made if default_who_made in WHO_MADE_CHOICES else "i_did"
        )
        shop.default_when_made = (
            default_when_made if default_when_made in WHEN_MADE_CHOICES else "made_to_order"
        )
        shop.default_is_supply = bool(default_is_supply)
        session.add(shop)
        session.commit()
    return RedirectResponse("/settings?saved=1", status_code=303)
