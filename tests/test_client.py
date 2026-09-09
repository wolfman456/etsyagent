import asyncio

import httpx
import pytest

from app.config import Settings
from app.etsy.client import EtsyClient, EtsyError, RateLimitExceeded


class FakeTokenStore:
    def __init__(self, token=None):
        self.token = token or {}
        self.saved = []

    def load(self):
        return self.token

    def save(self, token):
        self.token = {**self.token, **token}
        self.saved.append(self.token)


def make_settings(**overrides) -> Settings:
    base = {"etsy_keystring": "k", "etsy_shared_secret": "s"}
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def no_sleep(monkeypatch):
    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)


def build_client(handler, token=None, **kwargs):
    store = FakeTokenStore(token)
    transport = httpx.MockTransport(handler)
    client = EtsyClient(make_settings(), store, qps=1000, qpd=10000, **kwargs)
    client._client = httpx.AsyncClient(transport=transport)
    return client, store


@pytest.mark.asyncio
async def test_request_sends_headers(no_sleep):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["x_api_key"] = request.headers.get("x-api-key")
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"ok": True})

    client, _ = build_client(handler, token={"access_token": "AT"})
    body = await client.request("GET", "/users/me")
    assert body == {"ok": True}
    assert seen["url"] == "https://openapi.etsy.com/v3/application/users/me"
    assert seen["x_api_key"] == "k:s"
    assert seen["auth"] == "Bearer AT"


@pytest.mark.asyncio
async def test_request_401_refreshes_and_retries(no_sleep):
    calls = {"n": 0, "tokened": []}
    store = FakeTokenStore({"access_token": "OLD", "refresh_token": "RT"})

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        calls["tokened"].append(request.headers.get("authorization"))
        if request.headers.get("authorization") == "Bearer OLD":
            return httpx.Response(401, json={})
        return httpx.Response(200, json={"ok": True})

    client = EtsyClient(make_settings(), store, qps=1000, qpd=10000)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fake_refresh():
        store.save({"access_token": "NEW"})
        return True

    client._do_refresh = fake_refresh

    body = await client.request("GET", "/listings/1")
    assert body == {"ok": True}
    assert calls["n"] == 2
    assert calls["tokened"] == ["Bearer OLD", "Bearer NEW"]
    assert store.token["access_token"] == "NEW"


@pytest.mark.asyncio
async def test_request_429_retries_then_succeeds(no_sleep):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"retry-after": "1"}, json={})
        return httpx.Response(200, json={"done": True})

    client, _ = build_client(handler, token={"access_token": "AT"})
    body = await client.request("GET", "/listings/1")
    assert body == {"done": True}
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_request_exhausts_429_retries(no_sleep):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"retry-after": "1"}, json={})

    client, _ = build_client(handler, token={"access_token": "AT"})
    with pytest.raises(EtsyError) as excinfo:
        await client.request("GET", "/listings/1")
    assert excinfo.value.status == 429


@pytest.mark.asyncio
async def test_reserve_call_qpd_guard(no_sleep):
    store = FakeTokenStore({"access_token": "AT"})
    client = EtsyClient(make_settings(), store, qps=1000, qpd=2)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await client.request("GET", "/listings/1")
    await client.request("GET", "/listings/2")
    with pytest.raises(RateLimitExceeded):
        await client.request("GET", "/listings/3")


@pytest.mark.asyncio
async def test_request_no_token_raises(no_sleep):
    client, _ = build_client(lambda r: httpx.Response(200, json={}), token=None)
    with pytest.raises(EtsyError) as excinfo:
        await client.request("GET", "/users/me")
    assert excinfo.value.status == 401


@pytest.mark.asyncio
async def test_request_http_error_raised(no_sleep):
    client, _ = build_client(
        lambda r: httpx.Response(500, text="server exploded"),
        token={"access_token": "AT"},
    )
    with pytest.raises(EtsyError) as excinfo:
        await client.request("GET", "/listings/1")
    assert excinfo.value.status == 500


@pytest.mark.asyncio
async def test_update_listing_inventory_sends_query_param(no_sleep):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"products": []})

    client, _ = build_client(handler, token={"access_token": "AT"})
    body = await client.update_listing_inventory(123, {"products": []}, "3")
    assert body == {"products": []}
    assert "max_variations_supported=3" in seen["url"]
