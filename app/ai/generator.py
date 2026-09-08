from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings

MAX_TITLE_LEN = 140
MAX_TAGS = 13
MAX_TAG_LEN = 20
MAX_MATERIALS = 13
MAX_MATERIAL_LEN = 50


@dataclass
class DraftContent:
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    materials: list[str] = field(default_factory=list)


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


async def _call_openai(settings: Settings, prompt: str) -> str:
    headers = {"Authorization": f"Bearer {settings.openai_api_key}"}
    payload = {
        "model": settings.openai_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "response_format": {"type": "json_object"},
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{settings.openai_base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        body = response.json()
    return body["choices"][0]["message"]["content"]


async def _call_anthropic(settings: Settings, prompt: str) -> str:
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": settings.anthropic_model,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages", headers=headers, json=payload
        )
        response.raise_for_status()
        body = response.json()
    return "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")


async def generate_draft(settings: Settings, facts: ProductFacts) -> DraftContent:
    if settings.llm_provider is None:
        raise RuntimeError(
            "No LLM provider configured. Set OPENAI_API_KEY or ANTHROPIC_API_KEY in .env."
        )
    attributes_str = (
        json.dumps(facts.attributes, ensure_ascii=False) if facts.attributes else "(none)"
    )
    prompt = PROMPT_TEMPLATE.format(
        name=facts.name,
        taxonomy_path=facts.taxonomy_path or "(unspecified)",
        price=facts.price or "(unspecified)",
        who_made=facts.who_made,
        when_made=facts.when_made,
        is_supply="yes" if facts.is_supply else "no",
        attributes=attributes_str,
        notes=facts.notes or "(none)",
    )
    if settings.anthropic_api_key:
        raw = await _call_anthropic(settings, prompt)
    else:
        raw = await _call_openai(settings, prompt)
    parsed = extract_json(raw)
    return normalize_generated(parsed)
