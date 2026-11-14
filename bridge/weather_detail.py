"""The detailed weather card's data: now, the next hours, the next three days and a one-line heads up.

Open-Meteo (free, no key), for the place in config.json "weather", at most every 10 minutes. Temperatures
stay in Celsius (one decimal, so Fahrenheit rounds right) like the rest of the bridge's weather; the TV
home turns them into Fahrenheit when its unit says so. Times are the place's own (Open-Meteo answers in weather.timezone).

    GET /api/weather/detail ->
    {"now": {"temp", "feels", "code", "text", "is_day", "wind_mph"},
     "today": {"high", "low"},
     "hours": [{"label": "8 PM", "temp", "code", "pop"}, ...6],
     "days": [{"label": "Sun", "temp_high", "temp_low", "code", "pop"}, ...3],
     "note": "Rain likely after 10 PM" or ""}
"""
import datetime
import logging
import time

import config

log = logging.getLogger("weather")
EVERY = 600
_cache = {"at": 0, "data": None}

RAIN = set(range(51, 68)) | {80, 81, 82}
SNOW = set(range(71, 78)) | {85, 86}
STORM = {95, 96, 99}


def _hour_label(iso):
    h = int(iso[11:13])
    return f"{h % 12 or 12} {'AM' if h < 12 else 'PM'}"


def _day_label(iso):
    return datetime.date(int(iso[:4]), int(iso[5:7]), int(iso[8:10])).strftime("%a")


def shape(j, wmo):
    """Open-Meteo's answer -> the card's data (kept apart from the fetch so it can be tested)."""
    cur, hourly, daily = j["current"], j.get("hourly") or {}, j.get("daily") or {}
    now_iso = cur.get("time", "")[:13]   # "2026-11-14T19"
    code = int(cur.get("weather_code", -1))
    out = {
        "now": {"temp": round(cur["temperature_2m"], 1), "feels": round(cur.get("apparent_temperature", cur["temperature_2m"]), 1),
                "code": code, "text": wmo.get(code, ""), "is_day": bool(cur.get("is_day")),
                "wind_mph": round(cur.get("wind_speed_10m") or 0)},
        "today": {"high": None, "low": None}, "hours": [], "days": [], "note": "",
    }
    times = hourly.get("time") or []
    ahead = [i for i, t in enumerate(times) if t[:13] > now_iso]
    for i in ahead[:6]:
        out["hours"].append({"label": _hour_label(times[i]), "temp": round(hourly["temperature_2m"][i], 1),
                             "code": int(hourly["weather_code"][i]), "pop": int(hourly.get("precipitation_probability", [0] * len(times))[i] or 0)})
    # a heads up: the first of the next 12 hours that is likely wet, unless it already is
    if code not in RAIN | SNOW | STORM:
        for i in ahead[:12]:
            c, pop = int(hourly["weather_code"][i]), int((hourly.get("precipitation_probability") or [0] * len(times))[i] or 0)
            if pop >= 50 and c in RAIN | SNOW | STORM:
                kind = "Snow" if c in SNOW else "Storms" if c in STORM else "Rain"
                out["note"] = f"{kind} likely after {_hour_label(times[i])}"
                break
    elif code in RAIN | SNOW | STORM:
        for i in ahead[:12]:
            if int(hourly["weather_code"][i]) not in RAIN | SNOW | STORM:
                out["note"] = f"Clearing around {_hour_label(times[i])}"
                break
    days = daily.get("time") or []
    if days:
        out["today"] = {"high": round(daily["temperature_2m_max"][0], 1), "low": round(daily["temperature_2m_min"][0], 1)}
    for i in range(1, min(4, len(days))):
        out["days"].append({"label": _day_label(days[i]), "temp_high": round(daily["temperature_2m_max"][i], 1),
                            "temp_low": round(daily["temperature_2m_min"][i], 1), "code": int(daily["weather_code"][i]),
                            "pop": int((daily.get("precipitation_probability_max") or [0] * len(days))[i] or 0)})
    return out


async def detail(lat, lon, wmo):
    if _cache["data"] and time.time() - _cache["at"] < EVERY:
        return _cache["data"]
    import aiohttp
    tz = (config.WEATHER.get("timezone") or "auto").replace("/", "%2F")
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
           "&current=temperature_2m,apparent_temperature,weather_code,is_day,wind_speed_10m"
           "&hourly=temperature_2m,weather_code,precipitation_probability"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
           f"&wind_speed_unit=mph&timezone={tz}&forecast_days=4")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.get(url) as r:
                j = await r.json()
        _cache["data"], _cache["at"] = shape(j, wmo), time.time()
    except Exception as e:   # keep the last good reading
        log.warning("weather detail: %s", e)
    return _cache["data"]
