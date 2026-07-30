"""Photograph lookup for places.

A separate endpoint rather than something the agent resolves inline, so a
reply is never held up waiting for pictures. The client renders the itinerary
immediately and fills in photographs as they arrive, which is also why this
is a GET: the browser and any CDN in front of it can cache them.

Results are resolved by `services/images.py`, which prefers real photographs
of the actual place and falls back to a clearly-labelled stand-in. The
`is_representative` flag travels with every result so the interface can be
honest about which is which.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.images import get_image_resolver

router = APIRouter(tags=["images"])


class PlaceImageOut(BaseModel):
    """One resolved photograph."""

    url: str = Field(description="Direct image URL, safe for an <img> tag.")
    tier: str = Field(
        description=(
            "Which source produced it: wikivoyage, wikipedia, commons_geo "
            "(all real photographs of the place) or loremflickr (a stand-in)."
        )
    )
    is_representative: bool = Field(
        description=(
            "True when this is NOT a photograph of this specific place, only "
            "of something similar. Label it in the interface rather than "
            "passing it off as the real thing."
        )
    )
    credit: str | None = None
    source_page: str | None = None


@router.get(
    "/images/place",
    response_model=PlaceImageOut,
    summary="Find a photograph for a place",
)
async def place_image(
    user: CurrentUser,
    name: str = Query(min_length=1, max_length=160, description="The place to illustrate."),
    destination: str | None = Query(
        default=None, max_length=120, description="Surrounding city, to disambiguate."
    ),
    latitude: float | None = Query(default=None, ge=-90, le=90),
    longitude: float | None = Query(default=None, ge=-180, le=180),
    kind: str | None = Query(
        default=None, max_length=40, description="restaurant, hotel, sight, …"
    ),
) -> PlaceImageOut:
    """Resolve the best available photograph for a place.

    Authenticated so the lookup cannot be used as an open image proxy by
    anyone who finds the URL.

    Args:
        user: The authenticated caller.
        name: Place name.
        destination: Surrounding city, when known.
        latitude: Venue latitude, when known.
        longitude: Venue longitude, when known.
        kind: Category hint for the fallback.

    Returns:
        A photograph, always - the final tier never fails.
    """
    resolved = await get_image_resolver().resolve(
        name,
        destination=destination,
        latitude=latitude,
        longitude=longitude,
        kind=kind,
    )
    return PlaceImageOut(
        url=resolved.url,
        tier=resolved.tier,
        is_representative=resolved.is_representative,
        credit=resolved.credit,
        source_page=resolved.source_page,
    )
