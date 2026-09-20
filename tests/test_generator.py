import httpx
import pytest

from app.ai.generator import (
    DraftContent,
    ProductFacts,
    _retry_on_429,
    build_openai_payload,
    cap_materials,
    cap_tags,
    extract_json,
    generate_draft,
    image_data_url,
    normalize_generated,
    split_data_url,
)
from app.config import Settings


def make_settings(**overrides) -> Settings:
    base = {"openai_api_key": "sk-test", "openai_model": "gpt-4o-mini"}
    base.update(overrides)
    return Settings(**base)


def test_cap_tags_limits_and_dedupes():
    tags = ["Wood", "wood", "VeryLongTagExceedingTwentyChars!!", "Handmade", "Wood", ""]
    result = cap_tags(tags)
    assert result == ["Wood", "Handmade"]


def test_build_openai_payload_json_mode_on():
    payload = build_openai_payload(make_settings(), "prompt")
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["temperature"] == 0.7
    assert payload["model"] == "gpt-4o-mini"


def test_build_openai_payload_json_mode_off():
    payload = build_openai_payload(make_settings(openai_json_mode=False), "prompt")
    assert "response_format" not in payload


def test_cap_tags_max_count():
    result = cap_tags([f"tag{i}" for i in range(20)])
    assert len(result) == 13


def test_cap_materials():
    assert cap_materials(["Oak", "oak", ""]) == ["Oak"]
    assert len(cap_materials([f"M{i}" for i in range(20)])) == 13


def test_normalize_generated_caps_title():
    long_title = "x" * 300
    draft = normalize_generated({"title": long_title, "description": "<p>hi</p>", "tags": ["a"]})
    assert len(draft.title) == 140
    assert draft.description == "<p>hi</p>"


def test_normalize_generated_missing_keys():
    draft = normalize_generated({})
    assert isinstance(draft, DraftContent)
    assert draft.tags == []


def test_extract_json_from_fence_and_trailing_text():
    raw = 'Here you go:\n```json\n{"title": "T", "tags": ["a"]}\n```\nEnjoy!'
    parsed = extract_json(raw)
    assert parsed == {"title": "T", "tags": ["a"]}


def test_extract_json_inline():
    assert extract_json('some text {"k": 1} more') == {"k": 1}


def test_extract_json_invalid_raises():
    with pytest.raises(ValueError):
        extract_json("no json here")


@pytest.mark.asyncio
async def test_generate_draft_no_provider_raises():
    with pytest.raises(RuntimeError):
        await generate_draft(Settings(openai_api_key=""), ProductFacts(name="Mug"))


@pytest.mark.asyncio
async def test_generate_draft_openai(monkeypatch):
    async def fake_openai(settings, prompt, images=None):
        assert "Mug" in prompt
        assert images is None
        return '{"title": "Title", "description": "<p>Desc</p>", "tags": ["a"]}'

    monkeypatch.setattr("app.ai.generator._call_openai", fake_openai)
    draft = await generate_draft(
        make_settings(), ProductFacts(name="Mug", taxonomy_path="Home > Mugs")
    )
    assert draft.title == "Title"
    assert draft.tags == ["a"]


@pytest.mark.asyncio
async def test_generate_draft_anthropic(monkeypatch):
    async def fake_anthropic(settings, prompt, images=None):
        return '{"title": "T", "description": "<p>D</p>", "tags": []}'

    monkeypatch.setattr("app.ai.generator._call_anthropic", fake_anthropic)
    draft = await generate_draft(
        make_settings(anthropic_api_key="ant-test"), ProductFacts(name="Mug")
    )
    assert draft.title == "T"


def test_image_data_url_and_split(tmp_path):
    img = tmp_path / "photo.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfakepng")
    url = image_data_url(img)
    assert url.startswith("data:image/png;base64,")
    media, data = split_data_url(url)
    assert media == "image/png"
    assert data == url.split(",", 1)[1]


def test_build_openai_payload_with_images():
    url = "data:image/jpeg;base64,AAAA"
    payload = build_openai_payload(make_settings(), "Look at this", [url])
    content = payload["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "Look at this"}
    assert content[1] == {"type": "image_url", "image_url": {"url": url}}


def test_normalize_generated_extended_fields():
    draft = normalize_generated(
        {
            "name": "Ceramic Mug",
            "price": 24.99,
            "category": "Home & Living > Kitchen & Dining",
            "title": "Mug",
            "description": "<p>d</p>",
            "tags": ["a"],
        }
    )
    assert draft.name == "Ceramic Mug"
    assert draft.price == "24.99"
    assert draft.category == "Home & Living > Kitchen & Dining"


@pytest.mark.asyncio
async def test_generate_draft_passes_photos(monkeypatch, tmp_path):
    img = tmp_path / "photo.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfakepng")

    async def fake_openai(settings, prompt, images=None):
        assert images and images[0].startswith("data:image/png;base64,")
        assert "photo of the item" in prompt
        return (
            '{"name": "Mug", "price": "24.99", "title": "T", "description": "<p>D</p>", '
            '"tags": []}'
        )

    monkeypatch.setattr("app.ai.generator._call_openai", fake_openai)
    draft = await generate_draft(make_settings(), ProductFacts(name=""), images=[img])
    assert draft.name == "Mug"
    assert draft.price == "24.99"


@pytest.mark.asyncio
async def test_generate_draft_downscales_oversized_photo(monkeypatch, tmp_path):
    import os

    from PIL import Image

    img = tmp_path / "big.png"
    noise = Image.frombytes("RGB", (2048, 2048), os.urandom(2048 * 2048 * 3))
    noise.save(img, format="PNG")
    assert img.stat().st_size > 5 * 1024 * 1024

    async def fake_openai(settings, prompt, images=None):
        assert images, "oversized photo must be downscaled and sent"
        assert images[0].startswith("data:image/jpeg;base64,")
        payload = images[0].split(",", 1)[1]
        assert len(payload) * 3 // 4 <= 5 * 1024 * 1024
        return '{"name": "Mug", "price": "10", "title": "T", "description": "<p>D</p>", "tags": []}'

    monkeypatch.setattr("app.ai.generator._call_openai", fake_openai)
    draft = await generate_draft(make_settings(), ProductFacts(name=""), images=[img])
    assert draft.name == "Mug"


@pytest.mark.asyncio
async def test_retry_on_429_backs_off_then_succeeds(monkeypatch):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("app.ai.generator.asyncio.sleep", fake_sleep)
    calls = {"n": 0}

    async def request():
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "2"}, json={})
        return httpx.Response(200, json={"ok": True})

    response = await _retry_on_429(request)
    assert response.status_code == 200
    assert calls["n"] == 2
    assert sleeps == [2]


@pytest.mark.asyncio
async def test_retry_on_429_uses_openai_reset_header(monkeypatch):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("app.ai.generator.asyncio.sleep", fake_sleep)
    calls = {"n": 0}

    async def request():
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"x-ratelimit-reset-requests": "7"}, json={})
        return httpx.Response(200, json={"ok": True})

    response = await _retry_on_429(request)
    assert response.status_code == 200
    assert sleeps == [7]


@pytest.mark.asyncio
async def test_retry_on_429_exponential_when_no_headers(monkeypatch):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("app.ai.generator.asyncio.sleep", fake_sleep)
    calls = {"n": 0}

    async def request():
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"ok": True})

    response = await _retry_on_429(request, backoff_max=10)
    assert response.status_code == 200
    assert calls["n"] == 3
    assert sleeps == [2, 4]


@pytest.mark.asyncio
async def test_retry_on_429_exhausts_retries(monkeypatch):
    async def fake_sleep(delay):
        pass

    monkeypatch.setattr("app.ai.generator.asyncio.sleep", fake_sleep)
    calls = {"n": 0}

    async def request():
        calls["n"] += 1
        return httpx.Response(429, json={})

    response = await _retry_on_429(request, max_retries=2)
    assert response.status_code == 429
    assert calls["n"] == 3
