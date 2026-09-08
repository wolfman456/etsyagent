from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from app.models import Product
from app.services.listing_builder import WHEN_MADE_CHOICES

REQUIRED_COLUMNS = {"name", "price"}

COLUMN_ALIASES = {
    "title": {"title"},
    "name": {"name", "product", "product_name"},
    "price": {"price"},
    "quantity": {"quantity", "qty"},
    "listing_type": {"listing_type", "type"},
    "description": {"description", "desc"},
    "tags": {"tags", "tag"},
    "materials": {"materials", "material"},
    "taxonomy_path": {"taxonomy_path", "category", "taxonomy"},
    "who_made": {"who_made"},
    "when_made": {"when_made"},
    "is_supply": {"is_supply"},
    "images": {"images", "photos"},
    "digital_file": {"digital_file", "file"},
}


@dataclass
class RowError:
    row: int
    message: str


@dataclass
class ImportResult:
    products: list[Product] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)


def _column_index(headers: list[str], field_name: str) -> int | None:
    aliases = COLUMN_ALIASES.get(field_name, {field_name})
    for index, header in enumerate(headers):
        if header.strip().lower() in {a.lower() for a in aliases}:
            return index
    return None


def _split(value: str) -> list[str]:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return parts


def _normalize_type(value: str) -> str:
    return "download" if value.strip().lower() in {"download", "digital"} else "physical"


def _normalize_when_made(value: str) -> str:
    v = value.strip().lower()
    if v in WHEN_MADE_CHOICES:
        return v
    for choice in WHEN_MADE_CHOICES:
        if choice.startswith(v[:4]):
            return choice
    return "made_to_order"


def _cell(row: list[str], idx: dict[str, int | None], field: str) -> str:
    index = idx[field]
    if index is None or index >= len(row):
        return ""
    return row[index].strip()


def parse_csv(content: str) -> ImportResult:
    reader = csv.reader(io.StringIO(content))
    try:
        raw_rows = list(reader)
    except csv.Error as exc:
        return ImportResult(errors=[RowError(0, f"CSV parse error: {exc}")])
    if not raw_rows:
        return ImportResult(errors=[RowError(0, "CSV is empty")])
    headers = [h.strip().lstrip("\ufeff") for h in raw_rows[0]]
    if not headers or not any(headers):
        return ImportResult(errors=[RowError(1, "CSV has no header row")])

    idx = {
        field: _column_index(headers, field)
        for field in COLUMN_ALIASES
    }
    missing = [name for name in REQUIRED_COLUMNS if idx[name] is None]
    if missing:
        return ImportResult(
            errors=[RowError(1, f"Missing required columns: {', '.join(sorted(missing))}")]
        )

    result = ImportResult()
    for row_number, row in enumerate(raw_rows[1:], start=2):
        if not row or not any(cell.strip() for cell in row):
            continue

        price = _cell(row, idx, "price")
        if price == "":
            result.errors.append(RowError(row_number, "missing price"))
            continue
        product = Product(
            name=_cell(row, idx, "name"),
            price=price,
            quantity=int(_cell(row, idx, "quantity") or 1),
            listing_type=_normalize_type(_cell(row, idx, "listing_type")),
            title=_cell(row, idx, "title"),
            description=_cell(row, idx, "description"),
            tags=_split(_cell(row, idx, "tags")),
            materials=_split(_cell(row, idx, "materials")),
            taxonomy_path=_cell(row, idx, "taxonomy_path"),
            who_made=_cell(row, idx, "who_made") or "i_did",
            when_made=_normalize_when_made(_cell(row, idx, "when_made")),
            is_supply=_cell(row, idx, "is_supply").strip().lower() in {"true", "1", "yes"},
            images=_split(_cell(row, idx, "images")),
            digital_file=_cell(row, idx, "digital_file") or None,
        )
        result.products.append(product)
    return result
