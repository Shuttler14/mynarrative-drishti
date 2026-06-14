from __future__ import annotations
import os
from pydantic_settings import BaseSettings, SettingsConfigDict


class ObsSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", env_file=".env", env_file_encoding="utf-8")

    env: str = "local"
    log_level: str = "INFO"
    log_json: bool = True
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = 0.1
    otel_endpoint: str = ""
    service_version: str = "dev"

    @classmethod
    def load(cls) -> ObsSettings:
        return cls(
            env=os.getenv("ENV", "local"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            log_json=os.getenv("ENV", "local") != "local",
            sentry_dsn=os.getenv("SENTRY_DSN", ""),
            sentry_traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            otel_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
            service_version=os.getenv("SERVICE_VERSION", "dev"),
        )
