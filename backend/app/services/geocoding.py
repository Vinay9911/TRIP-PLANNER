"""Shared place-name resolution via Open-Meteo's free geocoder.

Lives in `services/` rather than in `tools/weather.py`, where it was
originally written, because the mock flight/hotel provider needs the same
lookup and `providers/` sits *below* `tools/` in this project's layering
(`core -> services -> db -> providers -> tools -> ...`, see CLAUDE.md). A
provider importing from the tool layer would invert that rule, so the shared
piece belongs here instead, where both `providers/flights.py` and
`tools/weather.py` (and `tools/places.py`) can depend on it without either
depending on the other.

**Why this exists at all**, beyond the refactor: the mock provider's
`resolve_city` originally only checked a curated dict of ~70 major cities.
"Bali" - a whole island, and also just not one of the ~70 - returned
"not in the mock provider's city dataset" for both weather and accommodation
in a live run, even though the destination was perfectly real. Weather
already resolved arbitrary places through this geocoder; the fix is giving
the mock provider the same capability instead of a hand-maintained list that
can never cover every destination a traveller might name.
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings, get_settings
from app.services.http import request_json


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

    # Asking for more than one candidate and picking the most populous is a
    # real fix, not defensive padding: querying "Bali" with the previous
    # count=1 returned a hamlet called "Bāli" in West Bengal, India
    # (population ~297,000) ahead of the Indonesian island itself
    # (population ~4,225,000, present as the SECOND result) - a silent wrong
    # answer is worse than an honest failure, since nothing about a weather
    # forecast for the wrong country looks wrong until someone checks.
    # `count=1` is a trap for exactly this reason: it only works when the
    # geocoder's own internal ranking already agrees with "which Bali did a
    # traveller mean", and there is no reason to expect that for a name that
    # is shared by a global tourist destination and a small village.
    payload = await request_json(
        "GET",
        f"{cfg.open_meteo_geocoding_url}/search",
        service="open-meteo-geocoding",
        params={"name": query, "count": 10, "language": "en", "format": "json"},
    )

    results = payload.get("results") or []
    if not results:
        return None

    # Some legitimate results (small hamlets, and Open-Meteo's own country-
    # level entries in a few cases) omit population entirely; missing counts
    # as 0 so a candidate that actually reports a population always outranks
    # one that does not, and among candidates that all lack it the API's own
    # top result (list order) still wins via the stable sort.
    top = max(results, key=lambda entry: entry.get("population") or 0)
    return {
        "name": top.get("name"),
        "country": top.get("country"),
        "region": top.get("admin1"),
        "latitude": top.get("latitude"),
        "longitude": top.get("longitude"),
        "timezone": top.get("timezone"),
        "population": top.get("population"),
    }
