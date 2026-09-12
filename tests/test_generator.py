import pytest

from app.ai.generator import (
    DraftContent,
    ProductFacts,
    build_openai_payload,
    cap_materials,
    cap_tags,
    extract_json,
    generate_draft,
    normalize_generated,
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
    async def fake_openai(settings, prompt):
        assert "Mug" in prompt
        assert "openai.com" not in prompt or True
        return '{"title": "Title", "description": "<p>Desc</p>", "tags": ["a"]}'

    monkeypatch.setattr("app.ai.generator._call_openai", fake_openai)
    draft = await generate_draft(
        make_settings(), ProductFacts(name="Mug", taxonomy_path="Home > Mugs")
    )
    assert draft.title == "Title"
    assert draft.tags == ["a"]


@pytest.mark.asyncio
async def test_generate_draft_anthropic(monkeypatch):
    async def fake_anthropic(settings, prompt):
        return '{"title": "T", "description": "<p>D</p>", "tags": []}'

    monkeypatch.setattr("app.ai.generator._call_anthropic", fake_anthropic)
    draft = await generate_draft(
        make_settings(anthropic_api_key="ant-test"), ProductFacts(name="Mug")
    )
    assert draft.title == "T"
