from contextlib import asynccontextmanager

from fastapi import FastAPI

from admin.api import analytics, health
from admin.sources.events import EventAggregates
from admin.sources.prometheus import PrometheusClient

try:
    from drishti_observability import setup as setup_obs
except ImportError:
    setup_obs = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.prom = PrometheusClient()
    app.state.events = EventAggregates()
    yield


app = FastAPI(title="DRISHTI Admin Analytics", version="0.1.0", lifespan=lifespan)

if setup_obs:
    setup_obs(app, "admin-svc")

app.include_router(health.router)
app.include_router(analytics.router)
