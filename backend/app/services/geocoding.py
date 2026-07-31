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

import math
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.services.http import request_json

logger = get_logger(__name__)


#: How far from the destination centre a resolved pin may sit before it is
#: treated as a mismatch rather than a day trip.
#:
#: Geocoders return a best-effort match for any string, and for an ambiguous
#: landmark name that match can be on another continent. A real Jaipur plan
#: placed "Jaigarh Fort" in Maharashtra, 1,000km away, and its zoological
#: garden in Myanmar - both rendered as confident pins. A wrong pin is worse
#: than a missing one, because the map looks authoritative either way.
#:
#: 300km is deliberately generous: it comfortably contains a day trip from any
#: city, and multi-city regional itineraries too, while still catching the
#: wrong-country class of error that actually occurs.
MAX_PIN_DISTANCE_KM: float = 300.0

EARTH_RADIUS_KM: float = 6371.0

#: Geoapify `match_type` values meaning "I could not find what you asked for,
#: so here is the city you mentioned alongside it".
#:
#: These are the dangerous results. A distance check cannot catch them,
#: because the fallback is by definition inside the very city being searched -
#: "Catskill Mountains, New York" comes back as Manhattan, 0km from the
#: centre, looking like the most confident pin on the map.
_QUALIFIER_ONLY_MATCHES = frozenset(
    {"match_by_city_or_disrict", "match_by_country_or_state", "match_by_postcode"}
)

#: How many candidates to ask for, so the nearest can be chosen.
#:
#: This is what actually resolves landmarks correctly, and it replaced a
#: confidence threshold that was calibrated on a coincidence. Measured for
#: "Birla Temple, Jaipur": the **correct** temple 4km away scores confidence
#: **0**, while two wrong ones 334km and 354km away both score 0.67. Same for
#: "Hawa Mahal" - the right answer, one kilometre out, scores 0. Confidence
#: does not rank POIs by correctness, and thresholding on it threw away real
#: temples while admitting distant impostors.
#:
#: Proximity does rank them. When the destination is known, the nearest
#: plausible match is the right one, and `MAX_PIN_DISTANCE_KM` still bounds
#: how wrong "nearest" is allowed to be.
LANDMARK_CANDIDATES = 5

#: Geoapify result types acceptable as a *destination centre*.
#:
#: A centre may legitimately be a country, a state or a city - "Kerala" is a
#: state and "Jaipur" is a city - but it must not be a shop or a street, which
#: is what an unfiltered search returns for an unrecognised region name.
_CENTRE_RESULT_TYPES = frozenset({"country", "state", "county", "city", "postcode", "suburb"})


def distance_km(first: tuple[float, float], second: tuple[float, float]) -> float:
    """Great-circle distance between two (latitude, longitude) points.

    Args:
        first: Latitude and longitude in degrees.
        second: Latitude and longitude in degrees.

    Returns:
        Distance in kilometres.
    """
    lat1, lon1 = math.radians(first[0]), math.radians(first[1])
    lat2, lon2 = math.radians(second[0]), math.radians(second[1])
    d_lat, d_lon = lat2 - lat1, lon2 - lon1

    a = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


async def geocode_place(place: str, *, settings: Settings | None = None) -> dict[str, Any] | None:
    """Resolve a place name to coordinates and a timezone.

    **Geoapify is asked first, and this is a correctness fix rather than a
    preference.** Open-Meteo's gazetteer is wrong for exactly the destinations
    travellers ask about, and wrong *silently* - it answers confidently with
    somewhere else. Measured against the live API:

    ==================  =============================  ==================
    Asked for           Open-Meteo answered            Actually
    ==================  =============================  ==================
    ``Goa``             44.40, 8.94 - **Italy**        15.30, 74.09
    ``Manali``          13.17, 80.27 - a Chennai       32.25, 77.19
                        suburb
    ``Uttar Pradesh``   nothing at all                 27.13, 80.86
    ``Coorg``           nothing at all                 12.38, 75.66
    ==================  =============================  ==================

    Every one of those became a weather forecast for the wrong place,
    presented as if it were the right one - a forecast for Italy is not
    obviously wrong to someone who asked about Goa. Geoapify resolves all four
    correctly, and returns the country and timezone this needs as well.

    Open-Meteo remains the fallback, for when no Geoapify key is configured.

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

    resolved = await _geocode_place_geoapify(place, cfg)
    if resolved is not None:
        return resolved

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


async def _geocode_place_geoapify(place: str, cfg: Settings) -> dict[str, Any] | None:
    """Resolve a place through Geoapify, in `geocode_place`'s shape.

    Args:
        place: The place name.
        cfg: Application settings.

    Returns:
        The same dict `geocode_place` returns, or None to fall through to
        Open-Meteo - no key, no match, or an unreachable service. Never
        raises: a failure here must degrade to the other geocoder rather than
        fail the weather tool outright.
    """
    key = cfg.geoapify_api_key.get_secret_value()
    if not key:
        return None

    try:
        payload = await request_json(
            "GET",
            "https://api.geoapify.com/v1/geocode/search",
            service="geoapify-geocode",
            params={"text": place, "limit": "5", "apiKey": key},
        )
    except Exception:  # noqa: BLE001 - see the docstring; falls through
        logger.info("geocoding.place_geoapify_failed", place=place)
        return None

    for feature in payload.get("features") or []:
        properties = feature.get("properties") or {}
        # Same filter as `geocode_centre`: a weather forecast wants a
        # settlement or region, not a restaurant that happens to share a name.
        if properties.get("result_type") not in _CENTRE_RESULT_TYPES:
            continue
        latitude, longitude = properties.get("lat"), properties.get("lon")
        if latitude is None or longitude is None:
            continue

        return {
            "name": properties.get("city") or properties.get("name") or place,
            "country": properties.get("country"),
            "region": properties.get("state"),
            "latitude": float(latitude),
            "longitude": float(longitude),
            "timezone": (properties.get("timezone") or {}).get("name"),
            "population": properties.get("population"),
        }

    return None


async def geocode_centre(
    place: str, *, settings: Settings | None = None
) -> tuple[float, float] | None:
    """Resolve a destination to the point a map should be centred on.

    Separate from `geocode_place` because they fail differently. Open-Meteo is
    a gazetteer of *populated places*, so an administrative region is often
    absent from it or present with no population - and "most populous wins"
    cannot break a tie between two candidates that both report nothing.
    Measured: `geocode_place("Kerala")` returns **Kerälä, Finland**, a village
    of unstated size, in preference to the Indian state of 33 million people,
    which Open-Meteo does not rank as a settlement at all.

    That mattered more than a wrong dot on a map. The centre is what every
    landmark lookup is then measured against, so a centre in Finland rejected
    all four correct Kerala pins for being 7,000km away - one bad lookup
    silently emptied the entire map.

    Geoapify returns Kerala as `type=state` with confidence 1, so it is asked
    instead, and its answer is only accepted if it really is a place of the
    right *kind*. `geocode_place` is deliberately left alone: weather and the
    flight provider give it city names, which is the job it does well.

    Args:
        place: A destination - city, region or country.
        settings: Settings override, for tests.

    Returns:
        A (latitude, longitude) pair, or None when nothing suitable matched.
        None must mean "do not filter by distance" upstream, not "show nothing".
    """
    cfg = settings or get_settings()
    key = cfg.geoapify_api_key.get_secret_value()
    if not key:
        return None

    try:
        payload = await request_json(
            "GET",
            "https://api.geoapify.com/v1/geocode/search",
            service="geoapify-geocode",
            # More than one, because the best match for a bare region name is
            # not always first and the type filter below does the choosing.
            params={"text": place, "limit": "5", "apiKey": key},
        )
    except Exception:  # noqa: BLE001 - a missing centre disables filtering, never fails a run
        logger.info("geocoding.centre_failed", place=place)
        return None

    for feature in payload.get("features") or []:
        properties = feature.get("properties") or {}
        if properties.get("result_type") not in _CENTRE_RESULT_TYPES:
            continue
        latitude, longitude = properties.get("lat"), properties.get("lon")
        if latitude is None or longitude is None:
            continue
        return float(latitude), float(longitude)

    return None


async def geocode_landmark(
    name: str,
    *,
    near: str | None = None,
    centre: tuple[float, float] | None = None,
    settings: Settings | None = None,
) -> tuple[float, float] | None:
    """Resolve a named landmark, venue or district to coordinates.

    Separate from `geocode_place` because the two answer different questions.
    Open-Meteo's geocoder is a gazetteer of *populated places* - excellent for
    "Kyoto", useless for "Shaniwar Wada", which it simply does not contain.
    Measured against a real Pune itinerary it resolved zero of eleven named
    stops, which is why the itinerary map was always empty.

    Geoapify indexes points of interest and resolved the same landmarks to
    within a street. It uses the key this project already configures for
    `find_places`, so this adds no new credential.

    **`centre` is what stops it lying.** Appending the city to the query is a
    hint, not a constraint: Geoapify still returns its single best global
    match, and for a landmark whose name is not unique that match can be
    anywhere. A live Jaipur plan put "Jaigarh Fort" in Maharashtra and its
    zoological garden in Myanmar - both as confident pins on the map. Passing
    the destination's own coordinates biases the search and lets an obviously
    distant result be thrown away.

    Args:
        name: The landmark, venue or district.
        near: Surrounding city, which disambiguates enormously - "Old Town"
            matches everywhere, "Old Town, Geneva" matches once.
        centre: The destination's (latitude, longitude), when known. Biases
            the search toward it and rejects matches beyond
            `MAX_PIN_DISTANCE_KM`.
        settings: Settings override, for tests.

    Returns:
        A (latitude, longitude) pair, or None when nothing matched, the match
        was implausibly far from `centre`, or the lookup failed. Never raises:
        a missing pin is a cosmetic loss, and a wrong pin is worse than no pin.
    """
    cfg = settings or get_settings()
    key = cfg.geoapify_api_key.get_secret_value()
    if not key:
        return None

    query = f"{name}, {near}" if near else name
    params = {"text": query, "limit": str(LANDMARK_CANDIDATES), "apiKey": key}

    if centre is not None:
        # Geoapify orders bias coordinates lon,lat - the opposite of the
        # lat,lon this codebase passes everywhere else. Getting it backwards
        # is silent: the bias just points somewhere useless and results look
        # unbiased rather than wrong.
        params["bias"] = f"proximity:{centre[1]},{centre[0]}"

    try:
        payload = await request_json(
            "GET",
            "https://api.geoapify.com/v1/geocode/search",
            service="geoapify-geocode",
            params=params,
        )
    except Exception:  # noqa: BLE001 - see the docstring; pins are never fatal
        logger.info("geocoding.landmark_failed", name=name)
        return None

    # Each source ranks what it is actually good at: Geoapify's order decides
    # *relevance*, and the distance check decides *plausibility*. Reversing
    # that - picking the candidate nearest the centre - was tried and is
    # worse: it beats a good relevance ranking with raw proximity and moved
    # Amber Fort, which is genuinely 10km outside Jaipur, onto a nearer place
    # 15km from the real fort. So: walk their order, take the first that
    # survives both filters.
    rejected: tuple[str, float] | None = None

    for feature in payload.get("features") or []:
        properties = feature.get("properties") or {}
        latitude, longitude = properties.get("lat"), properties.get("lon")
        if latitude is None or longitude is None:
            continue

        # Geoapify says *what* it matched, and that is the difference between
        # a pin and a lie. Asked for "Catskill Mountains, New York" it cannot
        # find the Catskills, so it falls back to the part it does recognise -
        # the city - and returns Manhattan with
        # match_type=match_by_city_or_disrict. The coordinate is real, the
        # answer is wrong, and no distance check can catch it because the
        # wrong answer is by construction *next to* the place being searched.
        #
        # A match that only resolved the qualifier is no match at all.
        if (properties.get("rank") or {}).get("match_type") in _QUALIFIER_ONLY_MATCHES:
            continue

        found = (float(latitude), float(longitude))

        if centre is None:
            return found

        separation = distance_km(centre, found)
        if separation <= MAX_PIN_DISTANCE_KM:
            return found

        # Remembered only so the log can say how far out the best match was,
        # which is the difference between "no such place" and "the geocoder
        # sent us to another country".
        if rejected is None:
            rejected = (name, separation)

    if rejected is not None:
        logger.info(
            "geocoding.landmark_rejected",
            name=name,
            distance_km=round(rejected[1]),
            limit_km=MAX_PIN_DISTANCE_KM,
        )
    else:
        logger.info("geocoding.landmark_unmatched", name=name)

    return None
