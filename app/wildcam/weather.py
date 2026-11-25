"""Weather at the moment of each clip, from Open-Meteo (free, no account).

Only asked for once a location is set on the settings page; nothing leaves the house before.
Per clip we keep the WMO weather code, whether it was day, and the air temperature of that hour.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from .db import Database
from .i18n import _

log = logging.getLogger(__name__)

FORECAST = "https://api.open-meteo.com/v1/forecast"  # also serves the last ~3 months
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
RECENT_DAYS = 80

# WMO weather code -> (Dutch label, translated when shown, icon). Icons: clear, partly, cloudy, fog, drizzle, rain,
# snow, showers, thunder (drawn by media.py and the clip page alike).
CODES = {
    0: ("Helder", "clear"),
    1: ("Licht bewolkt", "partly"),
    2: ("Half bewolkt", "partly"),
    3: ("Bewolkt", "cloudy"),
    45: ("Mist", "fog"), 48: ("Mist", "fog"),
    51: ("Motregen", "drizzle"), 53: ("Motregen", "drizzle"), 55: ("Motregen", "drizzle"),
    56: ("Motregen", "drizzle"), 57: ("Motregen", "drizzle"),
    61: ("Lichte regen", "rain"), 63: ("Regen", "rain"), 65: ("Zware regen", "rain"),
    66: ("IJzel", "rain"), 67: ("IJzel", "rain"),
    71: ("Lichte sneeuw", "snow"), 73: ("Sneeuw", "snow"), 75: ("Zware sneeuw", "snow"), 77: ("Sneeuw", "snow"),
    80: ("Buien", "showers"), 81: ("Buien", "showers"), 82: ("Zware buien", "showers"),
    85: ("Sneeuwbuien", "snow"), 86: ("Sneeuwbuien", "snow"),
    95: ("Onweer", "thunder"), 96: ("Onweer", "thunder"), 99: ("Onweer", "thunder"),
}


def describe(code: int | None) -> tuple[str, str] | None:
    return None if code is None or code < 0 else CODES.get(code, ("Onbekend weer", "cloudy"))


def location(db: Database) -> tuple[float, float] | None:
    loc = db.setting("location", None)
    return (loc["lat"], loc["lon"]) if loc else None


def parse_location(text: str) -> tuple[float, float]:
    """'52.0, 5.8' -> (52.0, 5.8). Raises ValueError with a message in the user's language."""
    parts = [p for p in text.replace(";", ",").replace(" ", ",").split(",") if p]
    if len(parts) != 2:
        raise ValueError(_("Geef twee getallen: breedtegraad en lengtegraad, bijvoorbeeld 52.0, 5.8"))
    try:
        lat, lon = (float(p) for p in parts)
    except ValueError:
        raise ValueError(_("Geef twee getallen: breedtegraad en lengtegraad, bijvoorbeeld 52.0, 5.8")) from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError(_("Die coördinaten bestaan niet"))
    return lat, lon


def _day(lat: float, lon: float, day: date) -> dict[str, tuple[int, int, float]]:
    """Hourly (code, is_day, temperature) for one local day, keyed 'YYYY-MM-DDTHH'."""
    url = FORECAST if (date.today() - day).days <= RECENT_DAYS else ARCHIVE
    query = urllib.parse.urlencode({
        "latitude": f"{lat:.3f}", "longitude": f"{lon:.3f}", "timezone": "auto",
        "hourly": "weather_code,is_day,temperature_2m", "start_date": day.isoformat(), "end_date": day.isoformat(),
    })
    req = urllib.request.Request(f"{url}?{query}", headers={"User-Agent": "wildcam"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        hourly = json.load(resp)["hourly"]
    return {t[:13]: (c, d, temp) for t, c, d, temp in
            zip(hourly["time"], hourly["weather_code"], hourly["is_day"], hourly["temperature_2m"]) if c is not None}


def fill(db: Database, limit: int = 50) -> int:
    """Look up the weather for clips that have none yet. Returns how many got it."""
    loc = location(db)
    if not loc:
        return 0
    done, days = 0, {}
    for clip in db.clips_without_weather(limit):
        try:
            when = datetime.fromisoformat(clip["captured_at"])
        except (TypeError, ValueError):
            db.update_clip(clip["id"], weather_code=-1)
            continue
        if when > datetime.now(when.tzinfo) - timedelta(hours=1):
            continue  # the hour is not over yet; try later
        if when.date() not in days:
            try:
                days[when.date()] = _day(*loc, when.date())
            except (OSError, ValueError, KeyError) as exc:
                log.warning("Weather for %s not available: %s", when.date(), exc)
                return done  # offline or rate-limited: next round
        hour = days[when.date()].get(when.strftime("%Y-%m-%dT%H"))
        if hour is None:
            db.update_clip(clip["id"], weather_code=-1)
            continue
        code, is_day, temp = hour
        db.update_clip(clip["id"], weather_code=code, is_day=is_day, air_temp=temp)
        done += 1
    return done
