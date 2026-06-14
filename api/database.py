from __future__ import annotations

import ssl
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
    async_sessionmaker,
)

from api.config import get_settings

settings = get_settings()

# Strip sslmode from URL (asyncpg doesn't accept it as a URL param)
_db_url = settings.DATABASE_URL
parsed = urlparse(_db_url)
qs = parse_qs(parsed.query)
_needs_ssl = qs.pop("sslmode", None)
_db_url = urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))

_connect_args: dict = {}
if _needs_ssl:
    _connect_args["ssl"] = ssl.create_default_context()

engine = create_async_engine(
    _db_url,
    echo=settings.DEBUG,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    connect_args=_connect_args,
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
