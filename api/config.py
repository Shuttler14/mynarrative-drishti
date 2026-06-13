from __future__ import annotations

import os
from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "Drishti"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False

    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://drishti:drishti_secret@localhost:5432/drishti",
    )
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    QDRANT_URL: str = os.getenv("QDRANT_URL", "http://localhost:6333")

    JWT_SECRET: str = os.getenv("JWT_SECRET", "change-me-in-production")
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
        "http://localhost:3000",
        "http://localhost:8080",
    ]

    SCRAPING_RATE_LIMIT: int = 2
    SCRAPING_PROXY_URL: str = os.getenv("SCRAPING_PROXY_URL", "")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
