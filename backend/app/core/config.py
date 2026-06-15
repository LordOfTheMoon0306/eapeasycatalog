from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Smart Catalog"
    debug: bool = False
    log_level: str = "INFO"

    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    request_timeout_seconds: float = 12.0
    request_connect_timeout_seconds: float = 6.0
    max_retries: int = 3
    retry_backoff_seconds: float = 0.6
    max_items_per_source: int = 10
    source_concurrency_limit: int = 2
    source_min_interval_seconds: float = 0.25

    device_profile_default: str = "desktop"
    kaspi_device_profile: str = "mobile"
    ozon_device_profile: str = "mobile"
    wildberries_device_profile: str = "mobile"

    enable_block_telemetry: bool = True

    enable_playwright_fallback: bool = True
    apify_api_key: str = Field(default="", validation_alias=AliasChoices("APIFY_API_KEY", "APIFY_TOKEN"))
    apify_ozon_actor_id: str = "zen-studio/ozon-scraper-pro"
    apify_wildberries_actor_id: str = "akoinc/wb-card-parser"

    capmonster_api_key: str = Field(default="", validation_alias=AliasChoices("CAPMONSTER_API_KEY", "CAPMONSTER_KEY"))

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
