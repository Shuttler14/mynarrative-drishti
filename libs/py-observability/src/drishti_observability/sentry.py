from __future__ import annotations
import structlog

logger = structlog.get_logger(__name__)


def init(service: str, settings) -> bool:
    if not settings.sentry_dsn:
        logger.info("sentry.disabled", reason="no DSN")
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.env,
            release=f"{service}@{settings.service_version}",
            traces_sample_rate=settings.sentry_traces_sample_rate,
            send_default_pii=False,
            integrations=[StarletteIntegration(), FastApiIntegration()],
            before_send=_scrub,
        )
        logger.info("sentry.enabled", environment=settings.env)
        return True
    except Exception as e:
        logger.warning("sentry.init_failed", error=str(e))
        return False


def _scrub(event, hint):
    req = event.get("request", {})
    headers = req.get("headers", {})
    for h in list(headers):
        if h.lower() in ("authorization", "cookie", "x-shopify-hmac-sha256"):
            headers[h] = "***"
    return event
