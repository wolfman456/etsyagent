from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DIR = Path(os.environ.get("ETSYAGENT_DATA_DIR", Path.home() / ".config" / "etsyagent"))
DEFAULT_DIR.mkdir(parents=True, exist_ok=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    etsy_keystring: str = ""
    etsy_shared_secret: str = ""
    etsy_redirect_port: int = 8000
    # Public HTTPS base for the OAuth callback when hosted (e.g. Railway). Empty = localhost.
    public_base_url: str = ""

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    # Some OpenAI-compatible endpoints (e.g. local Ollama) reject response_format.
    openai_json_mode: bool = True

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-3-5-haiku-latest"

    data_dir: str = str(DEFAULT_DIR)

    @property
    def db_url(self) -> str:
        return f"sqlite:///{Path(self.data_dir) / 'etsyagent.db'}"

    @property
    def media_dir(self) -> Path:
        path = Path(self.data_dir) / "media"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def redirect_uri(self) -> str:
        if self.public_base_url:
            return f"{self.public_base_url.rstrip('/')}/callback"
        return f"http://localhost:{self.etsy_redirect_port}/callback"

    @property
    def etsy_credentials_set(self) -> bool:
        return bool(self.etsy_keystring and self.etsy_shared_secret)

    @property
    def llm_provider(self) -> str | None:
        if self.anthropic_api_key:
            return "anthropic"
        if self.openai_api_key:
            return "openai"
        return None


settings = Settings()
