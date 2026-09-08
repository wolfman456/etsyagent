from urllib.parse import parse_qs, urlparse

import httpx

from app.auth.oauth import (
    build_authorize_url,
    build_oauth_config,
    exchange_code,
    generate_pkce_pair,
    pkce_challenge,
)
from app.config import Settings


def make_settings(**overrides) -> Settings:
    base = {
        "etsy_keystring": "keystring123",
        "etsy_shared_secret": "secret456",
        "etsy_redirect_port": 8000,
    }
    base.update(overrides)
    return Settings(**base)


def test_pkce_challenge_is_s256():
    verifier, challenge = generate_pkce_pair()
    assert len(verifier) >= 43
    assert challenge == pkce_challenge(verifier)


def test_build_authorize_url_includes_params():
    cfg = build_oauth_config(make_settings(), ["listings_r", "listings_w"])
    url = build_authorize_url(cfg, "STATE123", "CHALLENGE456")
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "www.etsy.com"
    qs = parse_qs(parsed.query)
    assert qs["response_type"] == ["code"]
    assert qs["client_id"] == ["keystring123"]
    assert qs["redirect_uri"] == ["http://localhost:8000/callback"]
    assert qs["scope"] == ["listings_r listings_w"]
    assert qs["state"] == ["STATE123"]
    assert qs["code_challenge"] == ["CHALLENGE456"]
    assert qs["code_challenge_method"] == ["S256"]


def test_exchange_code_posts_token_request():
    requested = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requested["url"] = str(request.url)
        requested["auth"] = (request.headers.get("authorization") or "")
        requested["data"] = request.content.decode()
        return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt"})

    import asyncio

    cfg = build_oauth_config(make_settings(), ["listings_r"])
    transport = httpx.MockTransport(handler)

    async def run_exchange():
        async with httpx.AsyncClient(transport=transport) as client:
            return await exchange_code(cfg, "AUTHCODE", "VERIFIER", client)

    body = asyncio.run(run_exchange())
    assert body["access_token"] == "at"
    assert requested["url"].endswith("/v3/public/oauth/token")
    assert "code=AUTHCODE" in requested["data"]
    assert "code_verifier=VERIFIER" in requested["data"]
    import base64

    expected_basic = base64.b64encode(b"keystring123:secret456").decode()
    assert expected_basic in requested["auth"]
