"""Runtime configuration with secrets kept out of serializable settings."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process settings loaded from environment variables and an optional local `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="COMPUTER_USE_",
        extra="ignore",
    )

    demo_origin: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8765")
    evidence_dir: Path = Path("evidence")
    runtime_dir: Path = Path("runtime")
    headless: bool = True
    provider: Literal["anthropic", "openai"] = "openai"
    model: str = "gpt-4.1-mini"
    max_steps: int = Field(default=20, ge=1, le=100)
    run_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")

    @property
    def origin(self) -> str:
        """Return the canonical demo origin without a trailing slash."""

        return str(self.demo_origin).rstrip("/")

    def provider_key(self) -> SecretStr | None:
        """Return the configured provider credential without exposing its value."""

        return self.anthropic_api_key if self.provider == "anthropic" else self.openai_api_key
