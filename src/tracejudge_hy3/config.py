"""Central configuration loaded from environment variables / .env.

Nothing here hardcodes a real endpoint, model name, or API key: all Hy3 connection
details must come from the environment so the project never bundles credentials.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Hy3 / OpenAI-compatible provider
    hy3_base_url: str | None = Field(default=None)
    hy3_api_key: str | None = Field(default=None)
    hy3_model: str | None = Field(default=None)
    hy3_reasoning_effort: str = Field(default="high")
    hy3_timeout_seconds: float = Field(default=120.0, gt=0)
    hy3_max_retries: int = Field(default=2, ge=0)
    hy3_max_parse_repairs: int = Field(default=1, ge=0, le=1)
    hy3_enable_reasoning_effort: bool = Field(default=True)

    # Sandbox
    tracejudge_sandbox: Literal["docker", "trusted-local"] = Field(default="docker")
    tracejudge_docker_image: str = Field(default="python:3.11-slim")
    tracejudge_test_timeout_seconds: float = Field(default=5.0, gt=0)
    tracejudge_memory_limit: str = Field(default="256m")
    tracejudge_cpu_limit: str = Field(default="1")
    tracejudge_artifact_dir: str = Field(default="artifacts")

    @property
    def artifact_path(self) -> Path:
        return Path(self.tracejudge_artifact_dir)

    def hy3_configured(self) -> bool:
        return bool(self.hy3_base_url and self.hy3_api_key and self.hy3_model)


def get_settings() -> Settings:
    """Load settings fresh from the environment each call.

    Not cached: tests and the CLI frequently mutate os.environ between calls
    (e.g. --provider hy3 vs mock), and a cached singleton would go stale.
    """

    return Settings()
