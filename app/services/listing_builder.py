from __future__ import annotations

import re
from itertools import product
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.etsy.client import EtsyClient
from app.etsy.money import dollars_to_minor, is_valid_price_string, parse_price
from app.models import Product, ShopProfile, SubmissionLog

MAX_VARIATION_DIMS = 2  # Etsy: "3" reported as coming soon
VARIATION_CAPS = {1: 70, 2: 4900, 3: 2500}

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
    if product.variations_list():
        build_inventory_payload(product, shop)


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


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "item"


def _auto_sku(product_name: str, labels: list[str]) -> str:
    parts = [_slugify(product_name), *(_slugify(p) for p in labels)]
    return "-".join(p for p in parts if p).upper()


def build_variant_matrix(
    variations: list[dict], product_name: str, base_price: str, base_qty: int
) -> list[dict]:
    """Cartesian product of variation values into SKU/price/qty variant rows."""
    dims = variations if isinstance(variations, list) else []
    if not 1 <= len(dims) <= MAX_VARIATION_DIMS:
        raise ListingValidationError(
            f"Variations support 1-{MAX_VARIATION_DIMS} dimensions "
            "(3 is coming soon on Etsy)."
        )
    value_lists = [d.get("values") or [] for d in dims]
    if any(not v for v in value_lists):
        raise ListingValidationError("Each variation dimension needs at least one value.")
    variants = []
    for combo in product(*value_lists):
        labels = list(combo)
        variants.append(
            {
                "sku": _auto_sku(product_name, labels),
                "price": base_price,
                "quantity": int(base_qty),
                "value_labels": labels,
            }
        )
    return variants


def build_inventory_payload(product: Product, shop: ShopProfile) -> dict[str, Any]:
    """Build the updateListingInventory body from the product's variations."""
    variations = product.variations_list()
    if not variations:
        return {}
    if not 1 <= len(variations) <= MAX_VARIATION_DIMS:
        raise ListingValidationError(
            f"Variations support 1-{MAX_VARIATION_DIMS} dimensions "
            "(3 is coming soon on Etsy)."
        )

    dims: list[dict] = []
    for dim in variations:
        property_id = dim.get("property_id")
        if not property_id:
            raise ListingValidationError("Each variation dimension needs a property.")
        values = [v.strip() for v in (dim.get("values") or []) if str(v).strip()]
        if not values:
            raise ListingValidationError(
                f"Variation '{dim.get('property_name') or property_id}' needs at least one value."
            )
        if any("(" in v or ")" in v for v in values):
            raise ListingValidationError("Variation values cannot contain parentheses.")
        dims.append(
            {
                "property_id": int(property_id),
                "property_name": dim.get("property_name") or f"Property {property_id}",
                "scale_id": dim.get("scale_id"),
                "values": values,
            }
        )

    expected = len(dims[0]["values"])
    for dim in dims[1:]:
        expected *= len(dim["values"])
    if expected > VARIATION_CAPS[len(dims)]:
        raise ListingValidationError(
            f"{expected} combinations exceeds Etsy's cap of {VARIATION_CAPS[len(dims)]} "
            f"for {len(dims)} variation dimension(s)."
        )

    variants = product.variants_list()
    if len(variants) != expected:
        raise ListingValidationError(
            f"Expected {expected} variant combination(s) but found {len(variants)}. "
            "Re-save the variation matrix."
        )

    base_price = parse_price(product.price)
    base_qty = int(product.quantity)
    seen_skus: set[str] = set()
    products: list[dict] = []
    price_varied = False
    qty_varied = False
    for variant in variants:
        labels = variant.get("value_labels") or []
        if len(labels) != len(dims):
            raise ListingValidationError("Each variant must carry one value per dimension.")
        for i, label in enumerate(labels):
            if label not in dims[i]["values"]:
                raise ListingValidationError(
                    "Variant value "
                    f"'{label}' is not part of variation '{dims[i]['property_name']}'."
                )
        sku = str(variant.get("sku") or "").strip()
        if not sku:
            sku = _auto_sku(product.name, labels)
        if sku in seen_skus:
            raise ListingValidationError(f"Duplicate SKU: {sku}. Each variant needs a unique SKU.")
        seen_skus.add(sku)
        price = str(variant.get("price") or "").strip() or product.price
        if not is_valid_price_string(price):
            raise ListingValidationError(f"Invalid variant price: {price!r}")
        quantity = int(variant.get("quantity") or 0)
        if quantity < 1:
            raise ListingValidationError(f"Variant '{sku}' needs a quantity of at least 1.")

        property_values = []
        for i, dim in enumerate(dims):
            entry: dict[str, Any] = {
                "property_id": dim["property_id"],
                "property_name": dim["property_name"],
                "value_ids": [],
                "values": [labels[i]],
            }
            if dim.get("scale_id"):
                entry["scale_id"] = int(dim["scale_id"])
            property_values.append(entry)

        offering: dict[str, Any] = {
            # NOTE: updateListingInventory prices are the Money FLOAT (dollars),
            # unlike createDraftListing.price which uses minor units.
            "price": float(parse_price(price)),
            "quantity": quantity,
            "is_enabled": True,
        }
        if product.listing_type == "physical" and shop.readiness_state_id:
            offering["readiness_state_id"] = shop.readiness_state_id

        products.append(
            {
                "sku": sku,
                "property_values": property_values,
                "offerings": [offering],
            }
        )
        if parse_price(price) != base_price:
            price_varied = True
        if quantity != base_qty:
            qty_varied = True

    payload: dict[str, Any] = {"products": products}
    dim_ids = [dim["property_id"] for dim in dims]
    if price_varied:
        payload["price_on_property"] = dim_ids
    if qty_varied:
        payload["quantity_on_property"] = dim_ids
    return payload


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

    if product.variations_list():
        log("inventory", f"{len(product.variations_list())} variation dimension(s)")
        await client.update_listing_inventory(
            listing_id, build_inventory_payload(product, shop)
        )

    log("publish", "state=active")
    await client.update_listing(shop.shop_id, listing_id, state="active")

    product.status = "published"
    product.error = ""
    session.add(product)
    session.commit()
    return product
