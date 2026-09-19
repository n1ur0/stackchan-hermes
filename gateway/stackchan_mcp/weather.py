"""Weather check for the Phase E notify-heartbeat (yorishiro fork).

Fetches the JMA (Japan Meteorological Agency) bosai feeds — no API key
required — and decides whether today's weather is worth a proactive
one-liner. Silence is the default: a normal day produces ``None``, and
the heartbeat says nothing.

Endpoints (``{office}`` is a JMA office code such as ``270000`` for
Osaka prefecture):

- ``https://www.jma.go.jp/bosai/warning/data/warning/{office}.json`` —
  active warnings/advisories per municipality (class20 code, e.g.
  ``2720900`` for Moriguchi City).
- ``https://www.jma.go.jp/bosai/forecast/data/forecast/{office}.json`` —
  short-term forecast including precipitation probabilities (pops).

Judgement is split into pure functions over the parsed JSON so the
speak/stay-silent matrix is unit-testable without the network.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

import aiohttp

from .http import get_json

logger = logging.getLogger(__name__)

JMA_WARNING_URL = "https://www.jma.go.jp/bosai/warning/data/warning/{office}.json"
JMA_FORECAST_URL = "https://www.jma.go.jp/bosai/forecast/data/forecast/{office}.json"

FETCH_TIMEOUT_S = 15

#: JMA warning/advisory codes → spoken names. Unknown codes fall back
#: to a generic phrase rather than being dropped, so a new code the
#: agency introduces still gets announced.
WARNING_NAMES = {
    # special warnings
    "32": "Blizzard Special Warning",
    "33": "Heavy Rain Special Warning",
    "35": "Storm Special Warning",
    "36": "Heavy Snow Special Warning",
    "37": "Wave Special Warning",
    "38": "Storm Surge Special Warning",
    # warnings
    "02": "Blizzard Warning",
    "03": "Heavy Rain Warning",
    "04": "Flood Warning",
    "05": "Storm Warning",
    "06": "Heavy Snow Warning",
    "07": "Wave Warning",
    "08": "Storm Surge Warning",
    # advisories
    "10": "Heavy Rain Advisory",
    "12": "Heavy Snow Advisory",
    "13": "Snowstorm Advisory",
    "14": "Thunderstorm Advisory",
    "15": "Strong Wind Advisory",
    "16": "Wave Advisory",
    "17": "Snowmelt Advisory",
    "18": "Flood Advisory",
    "19": "Storm Surge Advisory",
    "20": "Fog Advisory",
    "21": "Dry Air Advisory",
    "22": "Avalanche Advisory",
    "23": "Low Temperature Advisory",
    "24": "Frost Advisory",
    "25": "Icing Advisory",
    "26": "Snow Accretion Advisory",
}

#: JMA API status values arrive in Japanese; map them to English once
#: here so the rest of the code and fixtures stay ASCII. The keys are
#: \u escaped for that reason (status words: issued/continued/cancelled).
_JMA_STATUS_EN = {"\u767a\u8868": "issued", "\u7d99\u7d9a": "continued", "\u89e3\u9664": "cancelled"}

#: Statuses meaning the warning is in effect right now ("issued" /
#: "continued"). The "cancelled" entries linger in the feed and must
#: not trigger speech.
ACTIVE_STATUSES = frozenset({"issued", "continued"})


def active_warnings(warning_json: dict[str, Any], city_code: str) -> list[str]:
    """Names of warnings currently in effect for one municipality."""
    names: list[str] = []
    for area_type in warning_json.get("areaTypes", []):
        for area in area_type.get("areas", []):
            if area.get("code") != city_code:
                continue
            for warning in area.get("warnings", []):
                if _JMA_STATUS_EN.get(warning.get("status"), warning.get("status")) not in ACTIVE_STATUSES:
                    continue
                code = str(warning.get("code", ""))
                names.append(WARNING_NAMES.get(code, "weather advisory"))
    return names


def today_max_pop(
    forecast_json: list[Any], today: _dt.date
) -> int | None:
    """Max precipitation probability (%) among today's forecast slots.

    Returns None when the feed has no usable pops for today (past
    slots are published as empty strings).
    """
    best: int | None = None
    try:
        time_series = forecast_json[0]["timeSeries"]
    except (IndexError, KeyError, TypeError):
        return None
    for series in time_series:
        areas = series.get("areas") or []
        if not areas or "pops" not in areas[0]:
            continue
        pops = areas[0]["pops"]
        for when_s, pop_s in zip(series.get("timeDefines", []), pops):
            try:
                when = _dt.datetime.fromisoformat(when_s)
                pop = int(pop_s)
            except (ValueError, TypeError):
                continue
            if when.date() != today:
                continue
            if best is None or pop > best:
                best = pop
    return best


def judge_weather(
    warning_json: dict[str, Any],
    forecast_json: list[Any],
    *,
    city_code: str,
    pop_threshold: int,
    today: _dt.date,
) -> str | None:
    """One spoken line when the weather warrants it, else None.

    Priority: active warnings/advisories first, then a rain heads-up
    when today's precipitation probability reaches the threshold. A
    normal day returns None — the heartbeat stays silent.

    Phrasing is fixed templates for now; swapping in an LLM for the
    wording (detection stays deterministic) is a known extension point.
    """
    warnings = active_warnings(warning_json, city_code)
    if warnings:
        listed = ", ".join(warnings[:2])
        return f"Warnings active: {listed}. Stay safe!"

    pop = today_max_pop(forecast_json, today)
    if pop is not None and pop >= pop_threshold:
        return f"Rain likely today, {pop}% chance. Don't forget your umbrella."
    return None


async def check_weather(
    office_code: str,
    city_code: str,
    pop_threshold: int,
    *,
    today: _dt.date | None = None,
) -> str | None:
    """Fetch both feeds and judge. Raises on network/HTTP failure.

    Raising (rather than swallowing) lets the heartbeat distinguish
    "checked, nothing to say" (mark today as done) from "could not
    check" (leave the daily flag unset and retry on the next tick
    inside the window).
    """
    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        warning_json = await get_json(
            session, JMA_WARNING_URL.format(office=office_code)
        )
        forecast_json = await get_json(
            session, JMA_FORECAST_URL.format(office=office_code)
        )
    return judge_weather(
        warning_json,
        forecast_json,
        city_code=city_code,
        pop_threshold=pop_threshold,
        today=today or _dt.date.today(),
    )
