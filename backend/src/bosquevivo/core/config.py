from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "dev"
    database_url: str = "sqlite:///data/bosquevivo.db"
    data_dir: Path = Path("data")
    gemini_api_key: str = ""
    gemini_model: str = ""
    gemini_model_light: str = ""
    gee_project: str = ""
    google_application_credentials: str = ""
    satellite_provider: str = "gee"
    # versión del índice con la que se calculan las auditorías nuevas (v0 sigue disponible)
    index_version: str = "v1"
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_app_secret: str = ""
    phone_hmac_key: str = Field(default="dev-only-change-me")
    admin_token: str = ""
    agent_max_steps: int = 5
    agent_daily_limit_per_user: int = 30

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"


@lru_cache
def get_settings() -> Settings:
    return Settings()
