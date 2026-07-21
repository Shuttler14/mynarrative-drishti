from __future__ import annotations

import json
import os
from functools import lru_cache
from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "Drishti"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    ENV: str = os.getenv("ENV", "local")

    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://drishti:drishti@localhost:5432/drishti",
    )
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    QDRANT_URL: str = os.getenv("QDRANT_URL", "http://localhost:6333")

    JWT_SECRET: str = os.getenv("JWT_SECRET", "")
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRY_MINUTES: int = 60 * 24 * 7

    SHOPIFY_STORE_URL: str = os.getenv("SHOPIFY_STORE_URL", "https://mynarrative.in")
    SHOPIFY_ACCESS_TOKEN: str = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
    SHOPIFY_WEBHOOK_SECRET: str = os.getenv("SHOPIFY_WEBHOOK_SECRET", "")

    VTOE_GPU_URL: str = os.getenv("VTOE_GPU_URL", "http://localhost:8001")
    GAP_GPU_URL: str = os.getenv("GAP_GPU_URL", "http://localhost:8002")

    S3_BUCKET: str = os.getenv("S3_BUCKET", "mynarrative-dtf-bucket")
    AWS_ACCESS_KEY_ID: str = os.getenv("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY: str = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    AWS_REGION: str = os.getenv("AWS_REGION", "eu-north-1")

    SMTP_HOST: str = os.getenv("SMTP_HOST", "")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER: str = os.getenv("SMTP_USER", "")
    SMTP_PASS: str = os.getenv("SMTP_PASS", "")

    ORACLE_HOST: str = os.getenv("ORACLE_HOST", "localhost")
    CLOUDFLARE_TUNNEL_URL: str = os.getenv("CLOUDFLARE_TUNNEL_URL", "")

    CORS_ORIGINS: list[str] = [
        "https://mynarrative.in",
        "https://www.mynarrative.in",
        "https://mynarrative.store",
        "https://www.mynarrative.store",
        "https://jjdk0v-0c.myshopify.com",
        "http://localhost:3000",
        "http://localhost:8080",
    ]

    @model_validator(mode="before")
    @classmethod
    def _parse_cors(cls, values: dict) -> dict:
        raw = values.get("CORS_ORIGINS")
        if isinstance(raw, str):
            try:
                values["CORS_ORIGINS"] = json.loads(raw)
            except json.JSONDecodeError:
                values["CORS_ORIGINS"] = [o.strip() for o in raw.split(",") if o.strip()]
        return values

    OPENWEATHERMAP_API_KEY: str = os.getenv("OPENWEATHERMAP_API_KEY", "")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    REPLICATE_API_TOKEN: str = os.getenv("REPLICATE_API_TOKEN", "")

    # Replicate model versions (configurable, not hardcoded)
    REPLICATE_REMBG_VERSION: str = os.getenv("REPLICATE_REMBG_VERSION", "fb8af171cfa1616ddcf1242c093f9c46bcada5ad4cf6f2fbe8b81b330ec5c003")
    REPLICATE_VTON_VERSION: str = os.getenv("REPLICATE_VTON_VERSION", "0e122964dd5d7fce695da14e9206f8dd48c0c5595ecb7e3cf1a4078701fb2665")
    REPLICATE_LLaVA_VERSION: str = os.getenv("REPLICATE_LLaVA_VERSION", "80537f9eead1a5bfa72d5ac6ea6414379be41d4d4f6679fd776e9535d1eb58bb")
    REPLICATE_BLIP_VERSION: str = os.getenv("REPLICATE_BLIP_VERSION", "2e1dddc8621f72175f6da606e92dd76f1e24e0350828f98568c8b672e8e6583d")
    REPLICATE_FACE_BOUNDS_VERSION: str = os.getenv("REPLICATE_FACE_BOUNDS_VERSION", "913307a91a6c2850c0433b9c0a4ea12eb298e2a86e0e0bcbe6b334e8d78b1ea6")
    REPLICATE_COLOR_VERSION: str = os.getenv("REPLICATE_COLOR_VERSION", "3cd834532d85b3295a382a3c15625376e4bd728f8a48e06e395a78a05f42e006")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o")

    SCRAPING_RATE_LIMIT: int = int(os.getenv("SCRAPING_RATE_LIMIT", "2"))
    SCRAPING_PROXY_URL: str = os.getenv("SCRAPING_PROXY_URL", "")
    SCRAPING_CACHE_TTL: int = int(os.getenv("SCRAPING_CACHE_TTL", "86400"))
    SCRAPING_MIN_DELAY: float = float(os.getenv("SCRAPING_MIN_DELAY", "0.5"))

    @model_validator(mode="after")
    def _fail_closed_on_secrets(self) -> "Settings":
        weak = {"", "change-me-in-production", "secret", "changeme"}
        if self.ENV != "local":
            if self.JWT_SECRET in weak:
                raise RuntimeError(
                    "Refusing to start: JWT_SECRET is unset or uses a placeholder. "
                    "Set a strong random secret via JWT_SECRET env var."
                )
            if len(self.JWT_SECRET) < 32:
                raise RuntimeError(
                    "Refusing to start: JWT_SECRET must be at least 32 characters."
                )
            if not self.SHOPIFY_WEBHOOK_SECRET:
                raise RuntimeError(
                    "Refusing to start: SHOPIFY_WEBHOOK_SECRET is empty. "
                    "Set a strong random secret via SHOPIFY_WEBHOOK_SECRET env var."
                )
        return self

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
