from __future__ import annotations
import structlog

from admin.config import settings

logger = structlog.get_logger(__name__)


class EventAggregates:
    """ClickHouse aggregates for funnel KPIs that aren't in Prometheus (reco
    CTR, try-on→save conversion). Degrades gracefully if ClickHouse is absent."""

    async def reco_ctr(self, window_hours: int = 24) -> float:
        if not settings.clickhouse_dsn:
            return 0.0
        sql = f"""
        SELECT countIf(type='look_click') / nullIf(countIf(type='reco_impression'), 0)
        FROM events.clickstream
        WHERE ts > now() - INTERVAL {window_hours} HOUR
        """
        return await self._scalar(sql)

    async def tryon_to_save(self, window_hours: int = 24) -> float:
        if not settings.clickhouse_dsn:
            return 0.0
        sql = f"""
        SELECT countIf(type='look_save') / nullIf(countIf(type='tryon_completed'), 0)
        FROM events.clickstream
        WHERE ts > now() - INTERVAL {window_hours} HOUR
        """
        return await self._scalar(sql)

    async def _scalar(self, sql: str) -> float:
        try:
            import aiochclient, aiohttp
            async with aiohttp.ClientSession() as s:
                ch = aiochclient.ChClient(s, url=settings.clickhouse_dsn)
                row = await ch.fetchval(sql)
                return float(row or 0.0)
        except Exception as e:
            logger.warning("clickhouse.query_failed", error=str(e))
            return 0.0
