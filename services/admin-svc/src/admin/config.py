from __future__ import annotations
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ADMIN_", extra="ignore")
    env: str = Field(default="local", alias="ENV")
    prometheus_url: str = Field(default="http://prometheus.obs:9090", alias="PROMETHEUS_URL")
    clickhouse_dsn: str | None = Field(default=None, alias="CLICKHOUSE_DSN")


settings = Settings()
