"""Regression tests for a real wrong-answer bug in place resolution.

Querying Open-Meteo's free geocoder for "Bali" with the previous `count=1`
returned a hamlet in West Bengal, India (population ~297,000) ahead of the
Indonesian island itself (population ~4,225,000, present as the SECOND
result) - confirmed by querying the live API directly. That is a silent
wrong answer, which is worse than a failure: nothing about a weather
forecast for the wrong country looks wrong until someone checks it against
reality.

The fix asks for more candidates and picks the most populous, which is a
strong signal for "which place did a well-known destination name most
likely mean" without needing any curated list. These tests pin the fix with
a fabricated but structurally identical version of that exact response,
rather than depending on the live API's answer staying the same forever.

`resolve_city_async` extends the same fix to the mock flight/hotel provider,
which previously only knew a curated ~70-city dataset and reported "Bali"
as entirely unresolvable for both flights and accommodation.
"""

from __future__ import annotations

import httpx
import respx

from app.core.errors import ExternalServiceError
from app.providers.flights import resolve_city_async
from app.services.geocoding import geocode_place


@respx.mock
async def test_the_most_populous_candidate_wins_over_list_order(settings):
    """The exact bug: a small same-named village must not beat the real place."""
    respx.get(url__startswith=settings.open_meteo_geocoding_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "Bāli",
                        "country": "India",
                        "admin1": "West Bengal",
                        "latitude": 22.64859,
                        "longitude": 88.34115,
                        "timezone": "Asia/Kolkata",
                        "population": 296973,
                    },
                    {
                        "name": "Bali",
                        "country": "Indonesia",
                        "admin1": "Bali",
                        "latitude": -8.33333,
                        "longitude": 115.0,
                        "timezone": "Asia/Makassar",
                        "population": 4225384,
                    },
                    {
                        "name": "Bali",
                        "country": "China",
                        "admin1": "Gansu",
                        "latitude": 34.30613,
                        "longitude": 104.36915,
                        "timezone": "Asia/Shanghai",
                        "population": 7101,
                    },
                ]
            },
        )
    )

    place = await geocode_place("Bali", settings=settings)

    assert place is not None
    assert place["country"] == "Indonesia", (
        "the geocoder must prefer the island travellers mean, not whichever "
        "same-named hamlet the API happened to list first"
    )


@respx.mock
async def test_a_single_result_with_no_population_still_resolves(settings):
    """The common case - one match, no ambiguity - must keep working."""
    respx.get(url__startswith=settings.open_meteo_geocoding_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "Kyoto",
                        "country": "Japan",
                        "latitude": 35.0,
                        "longitude": 135.76,
                        "timezone": "Asia/Tokyo",
                    }
                ]
            },
        )
    )

    place = await geocode_place("Kyoto", settings=settings)

    assert place is not None
    assert place["name"] == "Kyoto"


@respx.mock
async def test_all_candidates_missing_population_falls_back_to_list_order(settings):
    """When nothing reports a population, the API's own top result still wins."""
    respx.get(url__startswith=settings.open_meteo_geocoding_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"name": "First", "latitude": 1.0, "longitude": 1.0},
                    {"name": "Second", "latitude": 2.0, "longitude": 2.0},
                ]
            },
        )
    )

    place = await geocode_place("Ambiguous", settings=settings)

    assert place is not None
    assert place["name"] == "First"


# ---------------------------------------------------------------------------
# resolve_city_async: the mock provider's fallback
# ---------------------------------------------------------------------------


async def test_a_dataset_city_resolves_without_any_network_call(settings, monkeypatch):
    """The fast path must not pay for a geocode when the dataset already knows the city."""
    from app.providers import flights as flights_module

    async def explode(*args, **kwargs):
        raise AssertionError("geocode_place should not be called for a dataset hit")

    monkeypatch.setattr(flights_module, "geocode_place", explode)

    resolved = await resolve_city_async("Kyoto", settings=settings)

    assert resolved is not None
    assert resolved[0] == "Kyoto"


async def test_a_dataset_miss_falls_back_to_a_live_geocode(settings, monkeypatch):
    """The real bug: Bali is not in the curated dataset but is a real place."""
    from app.providers import flights as flights_module

    async def fake_geocode(place, *, settings=None):
        assert place == "Bali"
        return {
            "name": "Bali",
            "country": "Indonesia",
            "latitude": -8.33333,
            "longitude": 115.0,
            "timezone": "Asia/Makassar",
            "population": 4225384,
        }

    monkeypatch.setattr(flights_module, "geocode_place", fake_geocode)

    resolved = await resolve_city_async("Bali", settings=settings)

    assert resolved is not None
    name, iata, latitude, longitude, tier = resolved
    assert name == "Bali"
    assert len(iata) == 3, "a synthetic placeholder code must still look like a 3-letter code"
    assert latitude == -8.33333
    assert longitude == 115.0
    assert tier == 3, "an unranked city gets the dataset's median tier, not an invented number"


async def test_a_genuinely_unknown_place_still_reports_unresolvable(settings, monkeypatch):
    from app.providers import flights as flights_module

    async def fake_geocode(place, *, settings=None):
        return None

    monkeypatch.setattr(flights_module, "geocode_place", fake_geocode)

    resolved = await resolve_city_async("Nowhereville", settings=settings)

    assert resolved is None


async def test_the_geocoder_being_down_degrades_to_unresolvable_not_a_crash(settings, monkeypatch):
    """A dead geocoding service must not be a harder failure than an unknown city."""
    from app.providers import flights as flights_module

    async def fake_geocode(place, *, settings=None):
        raise ExternalServiceError("geocoding is down", service="open-meteo-geocoding")

    monkeypatch.setattr(flights_module, "geocode_place", fake_geocode)

    resolved = await resolve_city_async("Bali", settings=settings)

    assert resolved is None
