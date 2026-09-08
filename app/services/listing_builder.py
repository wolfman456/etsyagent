from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.etsy.client import EtsyClient
from app.etsy.money import dollars_to_minor, is_valid_price_string
from app.models import Product, ShopProfile, SubmissionLog

WHEN_MADE_CHOICES = [
    "made_to_order",
    "2020_2026",
    "2010_2019",
    "2000_2009",
    "1990_1999",
    "1980_1989",
    "1970_1979",
    "1960_1969",
    "1950_1959",
    "1940_1949",
    "1930_1939",
    "1920_1929",
    "1910_1919",
    "1900_1909",
    "before_1900",
]
WHO_MADE_CHOICES = ["i_did", "someone_else", "collective"]


class ListingValidationError(ValueError):
    pass


def validate_listing_inputs(product: Product, shop: ShopProfile) -> None:
    if not product.name.strip():
        raise ListingValidationError("Product name is required.")
    if not product.title.strip():
        raise ListingValidationError("Listing title is required (generate it or write it).")
    if not product.description.strip():
        raise ListingValidationError("Listing description is required.")
    if not product.taxonomy_id:
        raise ListingValidationError("Category (taxonomy) is required.")
    if not is_valid_price_string(product.price):
        raise ListingValidationError(f"Invalid price: {product.price!r}")
    if product.listing_type == "physical":
        if not shop.ready_for_physical:
            raise ListingValidationError(
                "Physical listings need a shipping profile and a processing profile. "
                "Set them in Settings."
            )
    if product.quantity < 1:
        raise ListingValidationError("Quantity must be at least 1.")


def build_create_params(product: Product, shop: ShopProfile) -> dict[str, Any]:
    params: dict[str, Any] = {
        "quantity": product.quantity,
        "title": product.title,
        "description": product.description,
        "price": dollars_to_minor(product.price),
        "who_made": product.who_made if product.who_made in WHO_MADE_CHOICES else "i_did",
        "when_made": (
            product.when_made if product.when_made in WHEN_MADE_CHOICES else "made_to_order"
        ),
        "is_supply": int(bool(product.is_supply)),
        "taxonomy_id": product.taxonomy_id,
    }
    if product.listing_type == "physical":
        params["shipping_profile_id"] = shop.shipping_profile_id
        params["readiness_state_id"] = shop.readiness_state_id
    tags = product.tag_list()
    if tags:
        params["tags"] = ",".join(tags)
    materials = product.material_list()
    if materials:
        params["materials"] = ",".join(materials)
    return params


async def submit_listing(
    product: Product,
    shop: ShopProfile,
    client: EtsyClient,
    session: Session,
    media_dir: Path,
) -> Product:
    """Run the create → images/file → activate pipeline. Idempotent per listing_id."""
    validate_listing_inputs(product, shop)
    product.status = "submitting"
    session.add(product)
    session.commit()

    def log(step: str, detail: str = "") -> None:
        SubmissionLog.record(session, product.id, step, detail)

    if not product.etsy_listing_id:
        log("create", "createDraftListing")
        created = await client.create_draft_listing(
            shop.shop_id, **build_create_params(product, shop)
        )
        product.etsy_listing_id = created["listing_id"]
        log("created", f"listing_id={product.etsy_listing_id}")
        session.add(product)
        session.commit()

    listing_id = product.etsy_listing_id
    assert listing_id is not None

    for index, image_path in enumerate(product.image_list()):
        path = Path(image_path)
        if not path.exists():
            path = media_dir / path.name
        if not path.exists():
            raise FileNotFoundError(f"Image missing: {image_path}")
        log("image", path.name)
        await client.upload_listing_image(
            shop.shop_id, listing_id, path.read_bytes(), path.name, rank=index + 1
        )

    if product.listing_type == "download":
        if product.digital_file:
            path = Path(product.digital_file)
            if not path.exists():
                path = media_dir / path.name
            if not path.exists():
                raise FileNotFoundError(f"File missing: {product.digital_file}")
            log("file", path.name)
            await client.upload_listing_file(shop.shop_id, listing_id, path.read_bytes(), path.name)
        log("type", "download")
        await client.update_listing(shop.shop_id, listing_id, type="download")

    log("publish", "state=active")
    await client.update_listing(shop.shop_id, listing_id, state="active")

    product.status = "published"
    product.error = ""
    session.add(product)
    session.commit()
    return product
