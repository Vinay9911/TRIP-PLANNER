"""Weather forecasting via Open-Meteo.

Open-Meteo was chosen over OpenWeatherMap for one practical reason: it needs
no API key and no credit card. OpenWeatherMap's One Call 3.0 now requires a
card on file even to stay inside the free allowance, which is friction for
anyone cloning this repository to evaluate it. Open-Meteo is run by a
non-profit backed by national meteorological agencies, so the data provenance
is sound.

The tool resolves a place name to coordinates first (geocoding), then fetches
the daily forecast. That is two upstream calls behind one tool, which is
deliberate: asking a language model to supply latitude and longitude invites
confidently wrong coordinates, and "Kyoto" is a far more reliable argument
than "35.0116, 135.7681".

Forecast horizon is a real constraint worth surfacing rather than hiding.
Numerical weather prediction is useful to roughly 16 days out; beyond that the
tool returns climate normals instead and says so, because an agent presenting
a 60-day-out "forecast" as fact is worse than one admitting it does not know.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Final

from app.core.config import Settings, get_settings
from app.core.errors import ToolExecutionError
from app.services.http import request_json
from app.tools.base import ToolResult, resilient_tool

# Open-Meteo reports conditions as WMO 4677 codes. Translated here because
# `{"weather_code": 61}` is meaningless to a model composing an itinerary,
# whereas "slight rain" changes whether it suggests an outdoor market.
WMO_CODES: Final[dict[int, str]] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    56: "light freezing drizzle",
    57: "dense freezing drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "slight snow",
    73: "moderate snow",
    75: "heavy snow",
    77: "snow grains",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    85: "slight snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}

# Beyond this the model has no genuine skill; we switch to climate normals.
MAX_FORECAST_DAYS: Final[int] = 16

WEATHER_UNAVAILABLE: Final[str] = (
    "Weather data could not be retrieved for this location. Continue planning "
    "without weather-specific advice, and tell the user that forecasts were "
    "unavailable so they should check conditions themselves before travelling. "
    "Do not call this tool again for the same location in this conversation."
)


async def geocode_place(place: str, *, settings: Settings | None = None) -> dict[str, Any] | None:
    """Resolve a place name to coordinates and a timezone.

    Args:
        place: A city or place name, e.g. `"Kyoto"` or `"Kyoto, Japan"`.
        settings: Settings override, for tests.

    Returns:
        A dict with `name`, `country`, `latitude`, `longitude` and `timezone`,
        or None if the place could not be resolved.

    Raises:
        ExternalServiceError: If the geocoding service is unreachable.
    """
    cfg = settings or get_settings()

    # Open-Meteo's geocoder matches on the bare city name; a "City, Country"
    # string returns nothing. Splitting on the comma and searching for the
    # first component is what makes the natural argument a model would supply
    # actually work.
    query = place.split(",")[0].strip()

    payload = await request_json(
        "GET",
        f"{cfg.open_meteo_geocoding_url}/search",
        service="open-meteo-geocoding",
        params={"name": query, "count": 1, "language": "en", "format": "json"},
    )

    results = payload.get("results") or []
    if not results:
        return None

    top = results[0]
    return {
        "name": top.get("name"),
        "country": top.get("country"),
        "region": top.get("admin1"),
        "latitude": top.get("latitude"),
        "longitude": top.get("longitude"),
        "timezone": top.get("timezone"),
        "population": top.get("population"),
    }


def _parse_date(value: str, field: str) -> date:
    """Parse an ISO date, raising a model-actionable error if malformed.

    Args:
        value: Date string, expected as `YYYY-MM-DD`.
        field: Argument name, used in the error message.

    Returns:
        The parsed date.

    Raises:
        ToolExecutionError: If the value is not an ISO date. The message tells
            the model the exact expected format AND tells it to resolve vague
            phrases itself. The second half was added after a live run where
            the model passed the traveller's own words ('early October') as
            `start_date` eight times in a row - the format hint alone told it
            what an ISO date looks like, but not that converting the phrase
            was its job, so it kept resubmitting variations of the phrase.
    """
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ToolExecutionError(
            f"The `{field}` argument must be an ISO date formatted as YYYY-MM-DD "
            f"(for example 2026-08-14). Received: {value!r}. If you only know a "
            f"rough time like 'early October', resolve it to concrete dates "
            f"yourself (today is {date.today().isoformat()}) and call once with "
            "those - do not resubmit the phrase.",
            tool_name="get_weather_forecast",
        ) from exc


@resilient_tool(source="open-meteo", unavailable_message=WEATHER_UNAVAILABLE)
async def get_weather_forecast(
    location: str,
    start_date: str | None = None,
    end_date: str | None = None,
    settings: Settings | None = None,
) -> ToolResult:
    """Fetch the daily weather forecast for a location and date range.

    Args:
        location: City or place name, e.g. `"Kyoto"`.
        start_date: First day as `YYYY-MM-DD`. Defaults to today.
        end_date: Last day as `YYYY-MM-DD`. Defaults to `start_date` + 1 day,
            matching the two-day trips this agent plans.
        settings: Settings override, for tests.

    Returns:
        A `ToolResult` whose data contains the resolved location and a list of
        daily forecasts.
    """
    cfg = settings or get_settings()

    place = await geocode_place(location, settings=cfg)
    if place is None:
        return ToolResult.invalid(
            source="open-meteo",
            message=(
                f"Could not find a place named {location!r}. Ask the user to confirm the "
                "city, or try a better-known nearby city name."
            ),
        )

    today = date.today()
    start = _parse_date(start_date, "start_date") if start_date else today
    end = _parse_date(end_date, "end_date") if end_date else start + timedelta(days=1)

    if end < start:
        return ToolResult.invalid(
            source="open-meteo",
            message=(
                f"`end_date` ({end}) is before `start_date` ({start}). "
                "Call again with end_date on or after start_date."
            ),
        )

    # Past dates are a modelling error, not a forecast request.
    if end < today:
        return ToolResult.invalid(
            source="open-meteo",
            message=(
                f"The requested range ends on {end}, which is in the past. "
                "Ask the user for their actual travel dates."
            ),
        )

    horizon_days = (start - today).days
    beyond_horizon = horizon_days > MAX_FORECAST_DAYS

    if beyond_horizon:
        # No skill this far out. Rather than return nothing, report typical
        # conditions for the same calendar window and label them clearly, so
        # the model can say "expect roughly X in late October" honestly.
        return await _climate_normals(place, start, end)

    params: dict[str, Any] = {
        "latitude": place["latitude"],
        "longitude": place["longitude"],
        "daily": ",".join(
            [
                "weather_code",
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_sum",
                "precipitation_probability_max",
                "wind_speed_10m_max",
                "sunrise",
                "sunset",
            ]
        ),
        "timezone": place.get("timezone") or "auto",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }

    payload = await request_json(
        "GET", f"{cfg.open_meteo_base_url}/forecast", service="open-meteo", params=params
    )

    daily = payload.get("daily") or {}
    days: list[dict[str, Any]] = []
    for index, day in enumerate(daily.get("time", [])):
        code = _at(daily.get("weather_code"), index)
        days.append(
            {
                "date": day,
                "conditions": WMO_CODES.get(int(code), "unknown") if code is not None else None,
                "temp_max_c": _at(daily.get("temperature_2m_max"), index),
                "temp_min_c": _at(daily.get("temperature_2m_min"), index),
                "precipitation_mm": _at(daily.get("precipitation_sum"), index),
                "precipitation_chance_pct": _at(daily.get("precipitation_probability_max"), index),
                "wind_max_kmh": _at(daily.get("wind_speed_10m_max"), index),
                "sunrise": _at(daily.get("sunrise"), index),
                "sunset": _at(daily.get("sunset"), index),
            }
        )

    return ToolResult.ok(
        source="open-meteo",
        data={
            "location": f"{place['name']}, {place['country']}",
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "timezone": place.get("timezone"),
            "forecast_type": "forecast",
            "days": days,
        },
        message=(
            f"Daily forecast for {place['name']} from {start} to {end}. "
            "Use precipitation chance and temperature to decide between indoor and "
            "outdoor activities, and to advise on what to pack."
        ),
    )


async def _climate_normals(place: dict[str, Any], start: date, end: date) -> ToolResult:
    """Return typical conditions for dates beyond the forecast horizon.

    Uses the same calendar window from the previous year as a proxy for
    seasonal norms. This is an approximation and the result says so - the
    model must not present it as a forecast.

    Args:
        place: Resolved location from `geocode_place`.
        start: First requested day.
        end: Last requested day.

    Returns:
        A `ToolResult` with `forecast_type="climate_normals"`.
    """
    cfg = get_settings()
    last_year_start = start.replace(year=start.year - 1)
    last_year_end = end.replace(year=end.year - 1)

    payload = await request_json(
        "GET",
        "https://archive-api.open-meteo.com/v1/archive",
        service="open-meteo-archive",
        params={
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
            "timezone": place.get("timezone") or "auto",
            "start_date": last_year_start.isoformat(),
            "end_date": last_year_end.isoformat(),
        },
        timeout=float(cfg.llm_timeout_seconds),
    )

    daily = payload.get("daily") or {}
    highs = [value for value in (daily.get("temperature_2m_max") or []) if value is not None]
    lows = [value for value in (daily.get("temperature_2m_min") or []) if value is not None]
    rain = [value for value in (daily.get("precipitation_sum") or []) if value is not None]

    return ToolResult.ok(
        source="open-meteo-archive",
        data={
            "location": f"{place['name']}, {place['country']}",
            "forecast_type": "climate_normals",
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "typical_high_c": round(sum(highs) / len(highs), 1) if highs else None,
            "typical_low_c": round(sum(lows) / len(lows), 1) if lows else None,
            "typical_daily_rain_mm": round(sum(rain) / len(rain), 1) if rain else None,
        },
        message=(
            f"These dates are more than {MAX_FORECAST_DAYS} days away, which is beyond the "
            "reliable forecast horizon. The figures returned are typical conditions from the "
            "same dates last year, NOT a forecast. Present them as seasonal expectations and "
            "advise the user to check an actual forecast closer to departure."
        ),
    )


def _at(values: list[Any] | None, index: int) -> Any:
    """Safely read an index from a possibly-missing parallel array.

    Open-Meteo returns each daily variable as its own array. A requested
    variable that the API omits arrives as `None` rather than an array of
    nulls, so every read needs both a null check and a bounds check.

    Args:
        values: The array, or None.
        index: Position to read.

    Returns:
        The value, or None if unavailable.
    """
    if not values or index >= len(values):
        return None
    return values[index]
