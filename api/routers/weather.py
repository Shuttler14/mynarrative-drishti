from __future__ import annotations

import logging
from datetime import datetime, timezone

import os

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("drishti.weather")
router = APIRouter()

OPENWEATHER_URL = "https://api.openweathermap.org/data/2.5/weather"


class WeatherRequest(BaseModel):
    city: str
    country: str | None = None


class WeatherResponse(BaseModel):
    city: str
    country: str
    temp_c: float
    feels_like_c: float
    humidity: int
    description: str
    icon: str
    wind_speed: float
    condition: str
    fetched_at: str


_WEATHER_ICONS = {
    "Clear": "☀️", "Clouds": "☁️", "Rain": "🌧️", "Drizzle": "🌦️",
    "Thunderstorm": "⛈️", "Snow": "❄️", "Mist": "🌫️", "Haze": "🌫️",
    "Fog": "🌫️", "Smoke": "🌫️",
}


@router.post("/current")
async def get_current_weather(req: WeatherRequest):
    api_key = os.getenv("OPENWEATHERMAP_API_KEY", "")
    if not api_key:
        # Return error instead of fake data
        raise HTTPException(503, "Weather API key not configured. Set OPENWEATHERMAP_API_KEY env var.")

    params = {"q": f"{req.city},{req.country or ''}".rstrip(","), "units": "metric", "appid": api_key}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(OPENWEATHER_URL, params=params)
            if resp.status_code != 200:
                raise HTTPException(resp.status_code, f"Weather API error: {resp.text}")
            data = resp.json()

        main = data.get("main", {})
        wl = data.get("weather", [{}])
        wm = wl[0].get("main", "Clear") if wl else "Clear"
        wd = wl[0].get("description", "Clear Sky") if wl else "Clear Sky"
        wind = data.get("wind", {})

        return WeatherResponse(
            city=data.get("name", req.city),
            country=data.get("sys", {}).get("country", req.country or "IN"),
            temp_c=round(main.get("temp", 0), 1),
            feels_like_c=round(main.get("feels_like", 0), 1),
            humidity=main.get("humidity", 0),
            description=wd.title(),
            icon=_WEATHER_ICONS.get(wm, "🌤️"),
            wind_speed=wind.get("speed", 0),
            condition=wm,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )
    except httpx.TimeoutException:
        raise HTTPException(504, "Weather API timeout")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Weather fetch failed: {e}")
        raise HTTPException(500, "Failed to fetch weather")
