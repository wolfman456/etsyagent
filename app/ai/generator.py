from __future__ import annotations

import asyncio
import base64
import io
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings

MAX_TITLE_LEN = 140
MAX_TAGS = 13
MAX_TAG_LEN = 20
MAX_MATERIALS = 13
MAX_MATERIAL_LEN = 50
MAX_NAME_LEN = 80
MAX_PRICE_LEN = 16
MAX_CATEGORY_LEN = 120
# Anthropic caps images at 5MB each; OpenAI allows more. Be safe for both.
MAX_VISION_IMAGE_BYTES = 5 * 1024 * 1024
# Long edge target when downscaling oversize photos before encoding.
VISION_TARGET_DIMENSION = 1024

IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


@dataclass
class DraftContent:
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    materials: list[str] = field(default_factory=list)
    # Photo-driven extras; empty when generating from text facts only.
    name: str = ""
    price: str = ""
    category: str = ""


@dataclass
class ProductFacts:
    name: str
    taxonomy_path: str = ""
    listing_type: str = "physical"
    price: str = ""
    who_made: str = ""
    when_made: str = ""
    is_supply: bool = False
    notes: str = ""
    attributes: dict[str, str] = field(default_factory=dict)


def cap_tags(tags: list[str], *, limit: int = MAX_TAGS, max_len: int = MAX_TAG_LEN) -> list[str]:
    cleaned: list[str] = []
    for tag in tags:
        if not tag:
            continue
        t = tag.strip()
        if not t or len(t) > max_len:
            continue
        if len(cleaned) >= limit:
            break
        if t.lower() not in {c.lower() for c in cleaned}:
            cleaned.append(t)
    return cleaned


def cap_materials(materials: list[str]) -> list[str]:
    cleaned: list[str] = []
    for m in materials:
        if not m:
            continue
        m = m.strip()
        if not m or len(m) > MAX_MATERIAL_LEN:
            continue
        if len(cleaned) >= MAX_MATERIALS:
            break
        if m.lower() not in {c.lower() for c in cleaned}:
            cleaned.append(m)
    return cleaned


def normalize_generated(data: dict[str, Any]) -> DraftContent:
    """Validate/mangle raw LLM output so it always respects Etsy limits."""
    tags = data.get("tags") or []
    materials = data.get("materials") or []
    return DraftContent(
        title=str(data.get("title") or "")[:MAX_TITLE_LEN],
        description=str(data.get("description") or "").strip(),
        tags=cap_tags(tags),
        materials=cap_materials(materials),
        name=str(data.get("name") or "").strip()[:MAX_NAME_LEN],
        price=str(data.get("price") or "").strip()[:MAX_PRICE_LEN],
        category=str(data.get("category") or "").strip()[:MAX_CATEGORY_LEN],
    )


def extract_json(text: str) -> dict[str, Any]:
    """Extract the first JSON object from an LLM response (handles fences/text)."""
    text = text.strip()
    fence = re.match(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError("No JSON object found in LLM response") from None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON in LLM response") from exc


PROMPT_TEMPLATE = """You write Etsy product listings. Produce ONLY a JSON object with keys:
title (string, <= 140 chars, no emojis), description (string, 3-5 short HTML paragraphs
using <p>...</p>, keyword-rich), tags (array of <= 13 strings, each <= 20 chars, no
duplicates, mix of single and multi-word), materials (array of <= 13 strings).

Constraints: the item is sold on Etsy; "who made it" is {who_made}, "when" is {when_made},
supply/craft-supply item: {is_supply}. Do not invent claims about materials, origin facts,
or certifications unless provided.

Product:
- name: {name}
- category: {taxonomy_path}
- price: {price}
- attributes: {attributes}
- seller notes: {notes}
"""

VISUAL_PROMPT_TEMPLATE = """A photo of the item to sell on Etsy is attached. Produce ONLY a
JSON object with keys:
name (short product name, <= 80 chars), price (a reasonable retail USD price as a plain
number like "24.99"; use "" if you cannot suggest one), category (a short guess at the best
Etsy category as a broad keyword phrase like "Kitchen & Dining" or "Jewelry"; use "" if
unsure), title (string, <= 140 chars, no emojis), description (string, 3-5 short HTML
paragraphs using <p>...</p>, keyword-rich), tags (array of <= 13 strings, each <= 20 chars,
no duplicates), materials (array of <= 13 strings).

Rules: base everything on what is visible in the photo together with the seller facts
below — never invent hidden details, features, materials, or certifications you cannot see.
Facts (blank values are unknown and may be derived from the photo):
- name: {name}
- category: {taxonomy_path}
- price: {price}
- "who made it": {who_made}, "when": {when_made}, supply/craft-supply item: {is_supply}
- seller notes: {notes}
"""


def image_data_url(path: str | Path) -> str:
    text = Path(path).read_bytes()
    media_type = IMAGE_MEDIA_TYPES.get(Path(path).suffix.lower(), "image/jpeg")
    encoded = base64.b64encode(text).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def split_data_url(url: str) -> tuple[str, str]:
    """Return (media_type, base64 payload) for a data: URL (Anthropic's format)."""
    if "," not in url:
        raise ValueError("Invalid image data URL")
    mime, _, data = url.partition(",")
    mime = mime[len("data:") :]
    if mime.endswith(";base64"):
        mime = mime[: -len(";base64")]
    return mime, data


def build_openai_payload(
    settings: Settings, prompt: str, images: list[str] | None = None
) -> dict[str, Any]:
    message_content: Any = prompt
    if images:
        parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image_url in images:
            parts.append({"type": "image_url", "image_url": {"url": image_url}})
        message_content = parts
    payload: dict[str, Any] = {
        "model": settings.openai_model,
        "messages": [{"role": "user", "content": message_content}],
        "temperature": 0.7,
    }
    if settings.openai_json_mode:
        payload["response_format"] = {"type": "json_object"}
    return payload


async def _retry_on_429(
    request: Callable[[], Awaitable[httpx.Response]],
    *,
    max_retries: int = 4,
    backoff_max: int = 60,
) -> httpx.Response:
    """Send a request, retrying 429s with Retry-After backoff (mirrors EtsyClient)."""
    last: httpx.Response | None = None
    for attempt in range(max_retries + 1):
        last = await request()
        if last.status_code != 429 or attempt >= max_retries:
            break
        retry_after = last.headers.get("retry-after")
        try:
            delay = min(int(retry_after or 0), backoff_max)
        except (TypeError, ValueError):
            delay = 5
        if not delay:
            delay = 5
        await asyncio.sleep(delay)
    assert last is not None
    return last


async def _call_openai(
    settings: Settings, prompt: str, images: list[str] | None = None
) -> str:
    headers = {"Authorization": f"Bearer {settings.openai_api_key}"}
    payload = build_openai_payload(settings, prompt, images)
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await _retry_on_429(
            lambda: client.post(
                f"{settings.openai_base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
        )
        response.raise_for_status()
        body = response.json()
    return body["choices"][0]["message"]["content"]


async def _call_anthropic(
    settings: Settings, prompt: str, images: list[str] | None = None
) -> str:
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    content: list[dict[str, Any]] = []
    for image_url in images or []:
        media_type, data = split_data_url(image_url)
        content.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": data},
            }
        )
    content.append({"type": "text", "text": prompt})
    payload = {
        "model": settings.anthropic_model,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": content}],
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await _retry_on_429(
            lambda: client.post(
                "https://api.anthropic.com/v1/messages", headers=headers, json=payload
            )
        )
        response.raise_for_status()
        body = response.json()
    return "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")


def _format_facts(facts: ProductFacts) -> dict[str, str]:
    attributes_str = (
        json.dumps(facts.attributes, ensure_ascii=False) if facts.attributes else "(none)"
    )
    return {
        "name": facts.name,
        "taxonomy_path": facts.taxonomy_path or "(unspecified)",
        "price": facts.price or "(unspecified)",
        "who_made": facts.who_made,
        "when_made": facts.when_made,
        "is_supply": "yes" if facts.is_supply else "no",
        "attributes": attributes_str,
        "notes": facts.notes or "(none)",
    }


def _downscale_for_vision(path: Path) -> bytes | None:
    """Re-encode an oversized photo small enough for the vision APIs.

    Shrinks the long edge to VISION_TARGET_DIMENSION and re-encodes as JPEG,
    stepping quality down until the image fits the size cap. Returns None if
    Pillow is unavailable or the file cannot be decoded.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((VISION_TARGET_DIMENSION, VISION_TARGET_DIMENSION))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            for quality in (85, 70, 55, 40, 25):
                buffer = io.BytesIO()
                im.save(buffer, format="JPEG", quality=quality)
                if buffer.tell() <= MAX_VISION_IMAGE_BYTES:
                    return buffer.getvalue()
    except (OSError, ValueError):
        return None
    return None


def _to_data_urls(images: list[str | Path] | None) -> tuple[list[str], list[str]]:
    """Encode readable images, downscaling oversize ones. Returns (data_urls, skipped_sources)."""
    if not images:
        return [], []
    data_urls: list[str] = []
    skipped: list[str] = []
    for source in images:
        path = Path(source)
        try:
            if not path.exists():
                skipped.append(str(source))
                continue
            if path.stat().st_size <= MAX_VISION_IMAGE_BYTES:
                data_urls.append(image_data_url(path))
                continue
            compacted = _downscale_for_vision(path)
            if compacted is None:
                skipped.append(
                    f"{source} (over {MAX_VISION_IMAGE_BYTES // (1024 * 1024)}MB, "
                    "could not downscale)"
                )
                continue
            encoded = base64.b64encode(compacted).decode("ascii")
            data_urls.append(f"data:image/jpeg;base64,{encoded}")
        except OSError as exc:
            skipped.append(f"{source} ({exc})")
    return data_urls, skipped


async def generate_draft(
    settings: Settings, facts: ProductFacts, images: list[str | Path] | None = None
) -> DraftContent:
    if settings.llm_provider is None:
        raise RuntimeError(
            "No LLM provider configured. Set OPENAI_API_KEY or ANTHROPIC_API_KEY in .env."
        )
    data_urls, skipped = _to_data_urls(images)
    if images and not data_urls:
        raise RuntimeError("No usable photo for the AI: " + "; ".join(skipped))
    prompt = (VISUAL_PROMPT_TEMPLATE if data_urls else PROMPT_TEMPLATE).format(
        **_format_facts(facts)
    )
    if settings.anthropic_api_key:
        raw = await _call_anthropic(settings, prompt, data_urls or None)
    else:
        raw = await _call_openai(settings, prompt, data_urls or None)
    parsed = extract_json(raw)
    return normalize_generated(parsed)
