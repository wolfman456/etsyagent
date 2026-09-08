from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import httpx

from app.config import Settings

API_BASE = "https://openapi.etsy.com/v3/application"

TokenT = dict[str, Any]


class TokenStore:
    """Protocol: the client needs load/save of the token dict."""

    def load(self) -> TokenT | None:
        raise NotImplementedError

    def save(self, token: TokenT) -> None:
        raise NotImplementedError


class EtsyError(Exception):
    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class RateLimitExceeded(EtsyError):
    """App would exceed its rolling 24h quota; caller should fail fast, not hammer."""


class EtsyClient:
    def __init__(
        self,
        settings: Settings,
        token_store: TokenStore,
        *,
        qps: float = 5.0,
        qpd: int = 10000,
        backoff_max: int = 60,
        max_retries: int = 4,
        server: str = API_BASE,
    ) -> None:
        self.settings = settings
        self.token_store = token_store
        self.server = server.rstrip("/")
        self._lock = asyncio.Lock()
        self._next_call = 0.0
        self._backoff_max = backoff_max
        self._max_retries = max_retries
        self._qps = qps
        self._qpd = qpd
        self._call_times: deque[float] = deque()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def reserve_call(self, cost: int = 1) -> None:
        """Space out calls to respect QPS and fail fast if the 24h QPD budget is gone."""
        now = time.time()
        async with self._lock:
            while self._call_times and now - self._call_times[0] > 24 * 3600:
                self._call_times.popleft()
            if cost > self._qpd or len(self._call_times) + cost > self._qpd:
                raise RateLimitExceeded(
                    f"Etsy 24h quota guard hit ({len(self._call_times)}+{cost} >= {self._qpd})",
                    status=429,
                )
            if self._next_call > now:
                await asyncio.sleep(self._next_call - now)
            self._next_call = time.monotonic() + 1.0 / self._qps
            self._call_times.extend([time.time()] * cost)

    async def _send(self, method: str, path: str, *, headers: dict, **kwargs) -> httpx.Response:
        response = await self._client.request(
            method, f"{self.server}{path}", headers=headers, **kwargs
        )
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            try:
                delay = min(int(retry_after or 0), self._backoff_max)
            except (TypeError, ValueError):
                delay = 5
            if not delay:
                delay = 5
            return response, delay
        return response, 0

    def _auth_headers(self) -> dict[str, str]:
        headers = {
            "x-api-key": f"{self.settings.etsy_keystring}:{self.settings.etsy_shared_secret}",
        }
        token = self.token_store.load()
        if token and token.get("access_token"):
            headers["Authorization"] = f"Bearer {token['access_token']}"
        return headers

    async def _do_refresh(self) -> bool:
        token = self.token_store.load()
        if not token or not token.get("refresh_token"):
            raise EtsyError("OAuth token missing or expired; re-connect your shop.", status=401)
        client = httpx.AsyncClient(timeout=60.0)
        try:
            response = await client.post(
                "https://openapi.etsy.com/v3/public/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": token["refresh_token"],
                    "client_id": self.settings.etsy_keystring,
                    "redirect_uri": self.settings.redirect_uri,
                },
                auth=(self.settings.etsy_keystring, self.settings.etsy_shared_secret),
            )
            response.raise_for_status()
        finally:
            await client.aclose()
        body = response.json()
        merged = {**token, "access_token": body["access_token"]}
        if body.get("refresh_token"):
            merged["refresh_token"] = body["refresh_token"]
        merged["expires_in"] = body.get("expires_in")
        self.token_store.save(merged)
        return True

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: Any = None,
        data: dict | None = None,
        files: Any = None,
        needs_token: bool = True,
        cost: int = 1,
    ) -> Any:
        """Send a v3 request, retrying on 429 with backoff and once after a token refresh."""
        await self.reserve_call(cost=cost)
        attempt = 0
        last_error: EtsyError | None = None
        last_status: int | None = None
        while attempt <= self._max_retries:
            headers = self._auth_headers()
            if needs_token and not headers.get("Authorization"):
                raise EtsyError("No OAuth token stored; connect your shop first.", status=401)
            response, delay = await self._send(
                method, path, headers=headers, params=params, json=json, data=data, files=files
            )
            last_status = response.status_code
            if response.status_code == 429:
                attempt += 1
                last_error = EtsyError("Etsy rate limit exceeded", status=429, body=response.text)
                if attempt > self._max_retries:
                    break
                await asyncio.sleep(delay)
                continue
            if response.status_code == 401 and needs_token:
                try:
                    refreshed = await self._do_refresh()
                except Exception:  # noqa: BLE001
                    refreshed = False
                if refreshed:
                    attempt += 1
                    continue
            if response.status_code >= 400:
                raise EtsyError(
                    f"Etsy API {response.status_code} on {method} {path}: {response.text[:500]}",
                    status=response.status_code,
                    body=response.text,
                )
            return response.json()
        error = last_error or EtsyError(
            "Etsy request failed (too many retries)", status=last_status or 429
        )
        raise error

    # ---- user / shop -----------------------------------------------------

    async def get_user(self) -> Any:
        return await self.request("GET", "/users/me")

    async def get_user_shops(self) -> list[dict]:
        data = await self.request("GET", "/users/me/shops")
        return data.get("results", [])

    async def get_shop(self, shop_id: int) -> Any:
        return await self.request("GET", f"/shops/{shop_id}")

    # ---- taxonomy --------------------------------------------------------

    async def get_seller_taxonomy(self) -> list[dict]:
        """Public: full seller taxonomy tree, read-only, no token required."""
        data = await self.request("GET", "/seller-taxonomy/nodes", needs_token=False)
        return data.get("results", [])

    async def get_properties_by_taxonomy(self, taxonomy_id: int) -> list[dict]:
        data = await self.request(
            "GET", f"/seller-taxonomy/nodes/{taxonomy_id}/properties", needs_token=False
        )
        return data.get("results", [])

    # ---- shop profiles ---------------------------------------------------

    async def get_shop_shipping_profiles(self, shop_id: int) -> list[dict]:
        data = await self.request("GET", f"/shops/{shop_id}/shipping-profiles")
        return data.get("results", [])

    async def get_shop_processing_profiles(self, shop_id: int) -> list[dict]:
        data = await self.request("GET", f"/shops/{shop_id}/processing-profiles")
        return data.get("results", [])

    async def get_shop_sections(self, shop_id: int) -> list[dict]:
        data = await self.request("GET", f"/shops/{shop_id}/sections")
        return data.get("results", [])

    # ---- listings --------------------------------------------------------

    async def create_draft_listing(self, shop_id: int, **params: Any) -> dict:
        return await self.request("POST", f"/shops/{shop_id}/listings", data=params)

    async def update_listing(self, shop_id: int, listing_id: int, **params: Any) -> dict:
        return await self.request(
            "PATCH", f"/shops/{shop_id}/listings/{listing_id}", data=params
        )

    async def upload_listing_image(
        self,
        shop_id: int,
        listing_id: int,
        image_bytes: bytes,
        filename: str,
        *,
        rank: int | None = None,
    ) -> dict:
        files = {"image": (filename, image_bytes, _content_type(filename))}
        data = {}
        if rank is not None:
            data["rank"] = rank
        return await self.request(
            "POST", f"/shops/{shop_id}/listings/{listing_id}/images", data=data, files=files
        )

    async def upload_listing_file(
        self, shop_id: int, listing_id: int, file_bytes: bytes, filename: str
    ) -> dict:
        files = {"file": (filename, file_bytes, "application/octet-stream")}
        return await self.request(
            "POST", f"/shops/{shop_id}/listings/{listing_id}/files", files=files
        )

    async def get_listings_by_shop(
        self, shop_id: int, state: str | None = None, limit: int = 100, offset: int = 0
    ) -> dict:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if state:
            params["state"] = state
        return await self.request("GET", f"/shops/{shop_id}/listings", params=params)

    async def get_listing(self, listing_id: int) -> dict:
        return await self.request("GET", f"/listings/{listing_id}")

    async def delete_listing(self, listing_id: int) -> dict:
        return await self.request("DELETE", f"/listings/{listing_id}")

    async def update_listing_inventory(self, listing_id: int, payload: dict) -> dict:
        return await self.request(
            "PUT", f"/listings/{listing_id}/inventory", json=payload
        )


def _content_type(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith((".png", ".gif", ".webp")):
        return f"image/{lower.rsplit('.', 1)[1]}"
    if lower.endswith(".jpg") or lower.endswith(".jpeg"):
        return "image/jpeg"
    return "application/octet-stream"


class SqliteTokenStore(TokenStore):
    """Persists the OAuth token to the app's SQLite DB (single-row table)."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    def load(self) -> TokenT | None:
        from app.models import OAuthToken  # local import to avoid cycles

        with self._session_factory() as session:
            row = session.query(OAuthToken).order_by(OAuthToken.id.desc()).first()
            if not row:
                return None
            return {
                "access_token": row.access_token,
                "refresh_token": row.refresh_token,
                "expires_at": row.expires_at,
                "scopes": row.scopes,
            }

    def save(self, token: TokenT) -> None:
        from app.models import OAuthToken

        with self._session_factory() as session:
            row = session.query(OAuthToken).order_by(OAuthToken.id.desc()).first()
            if not row:
                row = OAuthToken(user_id="local")
            row.access_token = token.get("access_token", "")
            row.refresh_token = token.get("refresh_token", row.refresh_token)
            row.scopes = token.get("scopes", row.scopes)
            session.add(row)
            session.commit()
