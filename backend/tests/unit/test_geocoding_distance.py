"""Regression tests for the map-pin distance guard.

The bug these pin down is not "no pin" but "confidently wrong pin". A live
two-day Jaipur plan produced eleven markers, of which two were absurd:
Jaigarh Fort resolved into Maharashtra (~900km away) and the zoological
garden into Myanmar (~1,900km away). Both rendered identically to the nine
correct ones, so the map looked authoritative and was not.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services import geocoding
from app.services.geocoding import (
    MAX_PIN_DISTANCE_KM,
    distance_km,
    geocode_centre,
    geocode_landmark,
)

JAIPUR = (26.9124, 75.7873)


def test_distance_km_matches_known_separation() -> None:
    """Jaipur to Delhi is ~240km; anything wildly off means the maths is wrong."""
    delhi = (28.6139, 77.2090)
    assert 220 < distance_km(JAIPUR, delhi) < 280


def test_distance_km_is_zero_for_identical_points() -> None:
    """A point is not far from itself - guards against a sign or radians slip."""
    assert distance_km(JAIPUR, JAIPUR) == pytest.approx(0.0, abs=1e-6)


def _payload(latitude: float, longitude: float) -> dict[str, Any]:
    """Build a minimal Geoapify response.

    Args:
        latitude: Latitude the fake geocoder should return.
        longitude: Longitude the fake geocoder should return.

    Returns:
        A payload shaped like Geoapify's `/v1/geocode/search` response.
    """
    return {"features": [{"properties": {"lat": latitude, "lon": longitude}}]}


@pytest.mark.asyncio
async def test_far_match_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real Jaigarh Fort failure: a match 900km away must not become a pin."""
    monkeypatch.setattr(
        geocoding,
        "request_json",
        lambda *a, **k: _async(_payload(17.3011, 73.2213)),
    )

    result = await geocode_landmark("Jaigarh Fort", near="Jaipur", centre=JAIPUR)

    assert result is None


@pytest.mark.asyncio
async def test_nearby_match_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must not throw away correct pins - Amber Fort is 10km out."""
    monkeypatch.setattr(
        geocoding,
        "request_json",
        lambda *a, **k: _async(_payload(26.9855, 75.8513)),
    )

    result = await geocode_landmark("Amber Fort", near="Jaipur", centre=JAIPUR)

    assert result == (26.9855, 75.8513)


@pytest.mark.asyncio
async def test_day_trip_distance_survives(monkeypatch: pytest.MonkeyPatch) -> None:
    """A legitimate day trip sits inside the radius, not outside it.

    Chosen deliberately: the limit exists to catch wrong-country matches, and
    a limit tight enough to also reject Ajmer (~130km, a normal excursion from
    Jaipur) would be trading one wrong behaviour for another.
    """
    ajmer = (26.4499, 74.6399)
    assert distance_km(JAIPUR, ajmer) < MAX_PIN_DISTANCE_KM

    monkeypatch.setattr(geocoding, "request_json", lambda *a, **k: _async(_payload(*ajmer)))

    assert await geocode_landmark("Ajmer Sharif Dargah", near="Jaipur", centre=JAIPUR) == ajmer


@pytest.mark.asyncio
async def test_without_centre_the_result_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No centre means no claim about plausibility - the guard stays out of the way.

    This is the path taken when the destination itself cannot be geocoded, and
    it must keep working: degrading to the old behaviour is acceptable, failing
    closed and returning an empty map is not.
    """
    monkeypatch.setattr(
        geocoding,
        "request_json",
        lambda *a, **k: _async(_payload(17.3011, 73.2213)),
    )

    result = await geocode_landmark("Jaigarh Fort", near="Jaipur")

    assert result == (17.3011, 73.2213)


async def _async(value: Any) -> Any:
    """Wrap a value in a coroutine, so a lambda can stand in for an async call.

    Args:
        value: The value to return.

    Returns:
        The value, awaited.
    """
    return value


@pytest.mark.asyncio
async def test_a_city_fallback_match_is_not_a_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The New York failure: asked for the Catskills, given Manhattan.

    Geoapify could not find the Catskill Mountains, so it matched the only
    part it recognised - "New York" - and returned the city centre with
    `match_by_city_or_disrict`. The distance guard is useless here: the wrong
    answer sits 0km from the centre, making it look like the most confident
    pin on the map. Reading `match_type` is the only thing that catches it.
    """
    monkeypatch.setattr(
        geocoding,
        "request_json",
        lambda *a, **k: _async(
            {
                "features": [
                    {
                        "properties": {
                            "lat": 40.7127,
                            "lon": -74.0060,
                            "rank": {
                                "confidence": 0.25,
                                "match_type": "match_by_city_or_disrict",
                            },
                        }
                    }
                ]
            }
        ),
    )

    result = await geocode_landmark(
        "Catskill Mountains", near="New York", centre=(40.714, -74.006)
    )

    assert result is None


@pytest.mark.asyncio
async def test_a_distant_candidate_is_skipped_for_a_plausible_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Birla Temple case, and why confidence was the wrong signal.

    This replaces a test that rejected matches scoring below a confidence
    threshold. That rule was calibrated on a coincidence and is measurably
    wrong: asked for "Birla Temple, Jaipur", Geoapify returns the **correct**
    temple 4km away with confidence **0**, while two wrong temples 334km and
    354km away both score 0.67. "Hawa Mahal" behaves the same way.
    Thresholding on confidence discarded real landmarks and kept impostors.

    What works is walking the geocoder's own order and taking the first
    candidate that is geographically plausible - their ranking for relevance,
    ours for sanity. The distant entries here are listed first on purpose.
    """
    monkeypatch.setattr(
        geocoding,
        "request_json",
        lambda *a, **k: _async(
            {
                "features": [
                    {
                        "properties": {
                            "lat": 25.5186,
                            "lon": 78.7537,
                            "rank": {"confidence": 0.67, "match_type": "full_match"},
                        }
                    },
                    {
                        "properties": {
                            "lat": 29.9658,
                            "lon": 76.8273,
                            "rank": {"confidence": 0.67, "match_type": "full_match"},
                        }
                    },
                    {
                        "properties": {
                            "lat": 26.8922,
                            "lon": 75.8155,
                            "rank": {"confidence": 0.0, "match_type": "full_match"},
                        }
                    },
                ]
            }
        ),
    )

    assert await geocode_landmark("Birla Temple", near="Jaipur", centre=JAIPUR) == (
        26.8922,
        75.8155,
    )


@pytest.mark.asyncio
async def test_the_geocoders_own_ranking_is_respected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Among plausible candidates, first beats nearest.

    Picking the nearest instead was tried and is worse. Amber Fort is
    genuinely about 10km outside Jaipur, and preferring proximity moved it to
    a closer match 15km from the real fort - beating a good relevance ranking
    with raw distance. Both candidates below are within the limit, so the
    geocoder's order decides.
    """
    monkeypatch.setattr(
        geocoding,
        "request_json",
        lambda *a, **k: _async(
            {
                "features": [
                    {"properties": {"lat": 26.9855, "lon": 75.8513, "rank": {}}},
                    {"properties": {"lat": 26.8498, "lon": 75.7999, "rank": {}}},
                ]
            }
        ),
    )

    assert await geocode_landmark("Amber Fort", near="Jaipur", centre=JAIPUR) == (
        26.9855,
        75.8513,
    )


@pytest.mark.asyncio
async def test_a_confident_full_match_still_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guards must not reject the good case they were added around."""
    monkeypatch.setattr(
        geocoding,
        "request_json",
        lambda *a, **k: _async(
            {
                "features": [
                    {
                        "properties": {
                            "lat": 26.9855,
                            "lon": 75.8513,
                            "rank": {"confidence": 0.95, "match_type": "full_match"},
                        }
                    }
                ]
            }
        ),
    )

    assert await geocode_landmark("Amber Fort", near="Jaipur", centre=JAIPUR) == (
        26.9855,
        75.8513,
    )


@pytest.mark.asyncio
async def test_a_centre_must_be_a_place_not_a_shop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A region centre has to be a region, or the whole map is wrong.

    This is the Kerala bug. The centre is what every landmark is measured
    against, so one bad lookup does not misplace a pin - it rejects all of
    them. Open-Meteo answered "Kerala" with a Finnish village, and 7,000km
    later every correct Indian pin had been thrown away.
    """
    monkeypatch.setattr(
        geocoding,
        "request_json",
        lambda *a, **k: _async(
            {
                "features": [
                    {"properties": {"lat": 1.0, "lon": 2.0, "result_type": "amenity"}},
                    {"properties": {"lat": 10.3529, "lon": 76.5120, "result_type": "state"}},
                ]
            }
        ),
    )

    assert await geocode_centre("Kerala") == (10.3529, 76.5120)
