from __future__ import annotations

import base64
import hashlib
import os
import secrets
from dataclasses import dataclass

from httpx import AsyncClient

from app.config import Settings

AUTH_URL = "https://www.etsy.com/oauth/connect"
TOKEN_URL = "https://openapi.etsy.com/v3/public/oauth/token"


@dataclass
class OAuthConfig:
    keystring: str
    shared_secret: str
    redirect_uri: str
    scopes: list[str]


def build_oauth_config(settings: Settings, scopes: list[str]) -> OAuthConfig:
    return OAuthConfig(
        keystring=settings.etsy_keystring,
        shared_secret=settings.etsy_shared_secret,
        redirect_uri=settings.redirect_uri,
        scopes=scopes,
    )


def generate_pkce_pair() -> tuple[str, str]:
    """Return a (code_verifier, code_challenge) pair (RFC 7636, S256)."""
    verifier = base64.urlsafe_b64encode(os.urandom(48)).rstrip(b"=").decode("ascii")
    return verifier, pkce_challenge(verifier)


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_authorize_url(cfg: OAuthConfig, state: str, code_challenge: str) -> str:
    params = {
        "response_type": "code",
        "client_id": cfg.keystring,
        "redirect_uri": cfg.redirect_uri,
        "scope": " ".join(cfg.scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    from urllib.parse import urlencode

    return f"{AUTH_URL}?{urlencode(params)}"


async def exchange_code(
    cfg: OAuthConfig, code: str, code_verifier: str, client: AsyncClient
) -> dict:
    """Exchange the authorization code for tokens. Basic auth = keystring:shared_secret."""
    return await _token_grant(
        cfg,
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
        },
        client,
    )


async def refresh_token(
    cfg: OAuthConfig, refresh_token: str, client: AsyncClient
) -> dict:
    return await _token_grant(
        cfg,
        {"grant_type": "refresh_token", "refresh_token": refresh_token},
        client,
    )


async def _token_grant(
    cfg: OAuthConfig, form: dict[str, str], client: AsyncClient
) -> dict:
    form = {**form, "client_id": cfg.keystring, "redirect_uri": cfg.redirect_uri}
    auth = (cfg.keystring, cfg.shared_secret)
    response = await client.post(TOKEN_URL, data=form, auth=auth)
    response.raise_for_status()
    return response.json()


def new_state() -> str:
    return secrets.token_urlsafe(24)
