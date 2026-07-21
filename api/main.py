from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.config import get_settings
from api.database import engine

settings = get_settings()
logger = logging.getLogger("drishti")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting {settings.APP_NAME} v{settings.APP_VERSION}")
    # NOTE: Tables are created via Alembic migrations only (alembic upgrade head)
    # Base.metadata.create_all removed to prevent auto-creating tables in production
    yield
    await engine.dispose()
    logger.info("Shutting down")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
)

# --- Rate limiting + logging (added FIRST — innermost) ---
from api.middleware.rate_limit import RateLimitMiddleware, RequestLoggingMiddleware
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(RateLimitMiddleware, max_requests=int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "100")), window_seconds=int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60")))

# --- CORS (MUST be last add_middleware — outermost wrapper) ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Observability: structured logging + Sentry + request-id + Prometheus ---
try:
    from drishti_observability import setup as setup_obs
    from drishti_observability.config import ObsSettings
    setup_obs(app, "drishti-api", ObsSettings(
        env=settings.ENV,
        sentry_dsn="",  # set via SENTRY_DSN env var
        service_version=settings.APP_VERSION,
    ))
except ImportError:
    pass  # drishti-observability not installed — run without observability


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "detail": str(exc) if settings.DEBUG else None},
    )


from api.routers import user, catalog, analysis, look, reco, pricing, offer, webhooks, admin, vton, weather

app.include_router(user.router, prefix="/api/user", tags=["User"])
app.include_router(catalog.router, prefix="/api/catalog", tags=["Catalog"])
app.include_router(analysis.router, prefix="/api/analysis", tags=["Analysis"])
app.include_router(look.router, prefix="/api/look", tags=["Look"])
app.include_router(reco.router, prefix="/api/reco", tags=["Reco"])
app.include_router(pricing.router, prefix="/api/pricing", tags=["Pricing"])
app.include_router(offer.router, prefix="/api/offer", tags=["Offer"])
app.include_router(vton.router, prefix="/api/vton", tags=["VTON"])
app.include_router(webhooks.router, prefix="/api/webhooks", tags=["Webhooks"])
app.include_router(admin.router, prefix="/api/admin", tags=["Admin"])
app.include_router(weather.router, prefix="/api/weather", tags=["Weather"])


@app.get("/health")
async def health():
    return {"status": "ok", "service": settings.APP_NAME, "version": settings.APP_VERSION}


@app.get("/")
async def root():
    return {"name": settings.APP_NAME, "version": settings.APP_VERSION, "docs": "/docs"}
