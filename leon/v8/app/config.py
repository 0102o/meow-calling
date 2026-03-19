from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "intake-service"
    app_env: str = "dev"
    log_level: str = "INFO"

    database_path: str = "data/intake.db"

    openclaw_enabled: bool = False
    openclaw_hook_url: str = "http://127.0.0.1:18789/hooks/agent"
    openclaw_hook_token: str = ""
    openclaw_agent_id: str = "intake-hooks"
    openclaw_timeout_seconds: int = 60
    openclaw_deliver: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def database_path_obj(self) -> Path:
        return Path(self.database_path)


class OpenClawPayload(BaseModel):
    message: str
    agentId: str
    wakeMode: str = "now"
    deliver: bool = False
    timeoutSeconds: int = 60


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
