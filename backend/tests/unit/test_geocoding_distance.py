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
from app.services.geocoding import MAX_PIN_DISTANCE_KM, distance_km, geocode_landmark

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
