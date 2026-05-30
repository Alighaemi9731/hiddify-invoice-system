"""
Bootstrap configuration, loaded from environment / `.env`.

IMPORTANT: this holds ONLY the values needed to start the app (DB, secret key,
initial owner login, optional bot/payment bootstrap). Everything that the owner
should be able to change at runtime — bot token, wallet address, exchange rate,
message texts, pricing, reminder schedule, enforcement switch — lives in the
database `settings` table and is editable from the web panel. See
`app.services.settings_service` for runtime settings access.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ---- App ----
    app_env: str = "local"
    secret_key: str = "dev-insecure-change-me"
    access_token_expire_minutes: int = 720
    server_domain: str = ""  # set in production for CORS/HSTS

    # ---- Initial owner (seeded on first boot only) ----
    admin_username: str = "owner"
    admin_password: str = "change-me-now"

    # ---- Database ----
    postgres_user: str = "invoice"
    postgres_password: str = "invoice"
    postgres_db: str = "invoice"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    database_url: str | None = None  # overrides the parts above when set

    # ---- Telegram (bootstrap; runtime values live in DB settings) ----
    telegram_bot_token: str = ""
    announcement_channel_id: str = ""
    announcement_channel_link: str = ""

    # ---- Payments (BEP-20 USDT) bootstrap ----
    usdt_bep20_address: str = ""
    usdt_bep20_contract: str = "0x55d398326f99059fF775485246999027B3197955"
    bscscan_api_key: str = ""
    usdt_master_xpub: str = ""

    # ---- Pricing ----
    default_price_per_gb_toman: int = 1000
    toman_per_usdt: int = 70000

    # ---- Runtime toggles ----
    run_scheduler: bool = False  # the `backend` service sets this true

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def is_sqlite(self) -> bool:
        return self.sqlalchemy_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
