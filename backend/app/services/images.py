"""Photographs for places, resolved from keyless sources.

**Why no image API key.** Unsplash, Pexels and Pixabay all need a key, and
Unsplash additionally requires download-tracking calls and per-image
attribution in a prescribed format. This project already talks to Wikimedia
for its travel corpus, and Wikimedia serves images through the same API with
no key, no quota registration and no tracking obligation - so the primary
source costs nothing new operationally.

**Four tiers, most specific first.** Each falls through to the next:

1. ``wikivoyage`` - the destination article's own lead image. We already
   fetch these articles for retrieval, so the photo is of the place the
   traveller actually asked about.
2. ``wikipedia`` - search for a named landmark, take its page image. Verified
   working for things like "St. Pierre Cathedral" and "Jet d'Eau".
3. ``commons_geo`` - Commons geosearch within a small radius of a venue's
   coordinates. This is the one that covers restaurants and hotels, which
   have no encyclopaedia article but do have coordinates from Geoapify. The
   photo is genuinely of that spot, even if not of that business.
4. ``loremflickr`` - a keyword-matched Creative Commons photo. Not the place,
   but the right *kind* of place: "restaurant, geneva" returns a restaurant
   in Geneva. Keyless and deterministic via its lock parameter.

Tiers 1-3 return real photographs of the location. Tier 4 does not, and says
so: every result carries its ``tier`` and an ``is_representative`` flag, so
the interface can label pictures honestly rather than implying every image is
the venue in question.

`source.unsplash.com`, the old keyless Unsplash endpoint, was checked and is
gone - it returns 503. It is not used here.
"""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote

from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.services.http import request_json

logger = get_logger(__name__)

WIKIVOYAGE_API: Final[str] = "https://en.wikivoyage.org/w/api.php"
WIKIPEDIA_API: Final[str] = "https://en.wikipedia.org/w/api.php"
COMMONS_API: Final[str] = "https://commons.wikimedia.org/w/api.php"

#: Metres around a venue's coordinates to look for a photograph. Small on
#: purpose: widen it and you start returning pictures of a different street.
GEOSEARCH_RADIUS_M: Final[int] = 350

#: Words that make a keyword search worse rather than better - they describe
#: the itinerary rather than the subject.
_NOISE = re.compile(
    r"\b(the|a|an|and|or|of|in|at|to|for|your|our|visit|explore|see|"
    r"morning|afternoon|evening|day\s*\d+)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PlaceImage:
    """One resolved photograph.

    Attributes:
        url: Direct image URL, safe to use in an ``<img src>``.
        tier: Which resolver produced it - see the module docstring.
        is_representative: True when the photo is *not* of this specific
            place, only of something similar. The interface must label these
            rather than passing them off as the real thing.
        credit: Human-readable attribution, required by Wikimedia's licences.
        source_page: Where the image came from, for a credit link.
    """

    url: str
    tier: str
    is_representative: bool
    credit: str | None = None
    source_page: str | None = None


class ImageResolver:
    """Resolves place names and coordinates to photographs.

    Results are cached in-process. The same destination appears on every card
    of an itinerary and across turns of a conversation, so without a cache one
    reply would re-request the same photo a dozen times.

    Attributes:
        settings: Application settings.
    """

    def __init__(self, settings: Settings | None = None, *, cache_size: int = 512) -> None:
        """Initialise the resolver.

        Args:
            settings: Settings override, for tests.
            cache_size: How many resolutions to retain.
        """
        self.settings = settings or get_settings()
        self._cache: OrderedDict[str, PlaceImage] = OrderedDict()
        self._cache_size = cache_size

    # -- Public ------------------------------------------------------------

    async def resolve(
        self,
        name: str,
        *,
        destination: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        kind: str | None = None,
    ) -> PlaceImage:
        """Find the best available photograph for a place.

        Never raises and never returns None: a missing picture should leave a
        gap in a layout, not break a reply, so the final tier always yields
        something.

        Args:
            name: The place, e.g. "St. Pierre Cathedral" or "Geneva".
            destination: The surrounding city, used to disambiguate a name and
                to sharpen the keyword fallback.
            latitude: Venue latitude, when known.
            longitude: Venue longitude, when known.
            kind: Category hint - "restaurant", "hotel", "attraction" - used
                only by the keyword fallback.

        Returns:
            A `PlaceImage`, possibly only representative.
        """
        key = f"{name}|{destination}|{latitude}|{longitude}|{kind}"
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached

        resolved = await self._resolve_uncached(
            name, destination=destination, latitude=latitude, longitude=longitude, kind=kind
        )

        self._cache[key] = resolved
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return resolved

    # -- Tiers -------------------------------------------------------------

    async def _resolve_uncached(
        self,
        name: str,
        *,
        destination: str | None,
        latitude: float | None,
        longitude: float | None,
        kind: str | None,
    ) -> PlaceImage:
        """Try each tier in order of specificity."""
        cleaned = _clean(name)

        # A bare destination is best served by its own guide article.
        if destination is None or cleaned.lower() == _clean(destination).lower():
            found = await self._from_wikivoyage(cleaned)
            if found:
                return found

        found = await self._from_wikipedia(cleaned, destination)
        if found:
            return found

        if latitude is not None and longitude is not None:
            found = await self._from_commons_geo(latitude, longitude)
            if found:
                return found

        # Nothing specific exists. Return something of the right kind, clearly
        # flagged as not being this particular place.
        return self._keyword_fallback(cleaned, destination, kind)

    async def _from_wikivoyage(self, title: str) -> PlaceImage | None:
        """The destination article's lead image."""
        payload = await self._get(
            WIKIVOYAGE_API,
            "wikivoyage-images",
            {
                "action": "query",
                "prop": "pageimages",
                "piprop": "thumbnail",
                "pithumbsize": "800",
                "titles": title,
                "redirects": "1",
                "format": "json",
            },
        )
        url = _first_thumbnail(payload)
        if not url:
            return None
        return PlaceImage(
            url=url,
            tier="wikivoyage",
            is_representative=False,
            credit="Wikivoyage / Wikimedia Commons",
            source_page=f"https://en.wikivoyage.org/wiki/{quote(title.replace(' ', '_'))}",
        )

    async def _from_wikipedia(self, name: str, destination: str | None) -> PlaceImage | None:
        """Search Wikipedia for a named landmark, then take its page image."""
        query = f"{name} {destination}" if destination else name
        search = await self._get(
            WIKIPEDIA_API,
            "wikipedia-search",
            {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": "1",
                "format": "json",
            },
        )
        results = (search.get("query") or {}).get("search") or []
        if not results:
            return None

        title = results[0].get("title")
        if not title:
            return None

        # Wikipedia's search returns *something* for almost any string, so an
        # unguarded first hit is a false-positive machine: "Cafe du Soleil"
        # matched an unrelated article and would have been presented as a
        # genuine photograph of that restaurant. Require the found title to
        # actually overlap the name we searched for; anything weaker falls
        # through to a coordinate lookup or an openly-labelled stand-in.
        if not _titles_overlap(name, str(title)):
            logger.info("images.wikipedia_match_rejected", wanted=name, got=title)
            return None

        payload = await self._get(
            WIKIPEDIA_API,
            "wikipedia-images",
            {
                "action": "query",
                "prop": "pageimages",
                "piprop": "thumbnail",
                "pithumbsize": "800",
                "titles": title,
                "format": "json",
            },
        )
        url = _first_thumbnail(payload)
        if not url:
            return None
        return PlaceImage(
            url=url,
            tier="wikipedia",
            is_representative=False,
            credit=f"Wikipedia: {title}",
            source_page=f"https://en.wikipedia.org/wiki/{quote(str(title).replace(' ', '_'))}",
        )

    async def _from_commons_geo(self, latitude: float, longitude: float) -> PlaceImage | None:
        """A Commons photograph taken near these coordinates.

        The path that covers restaurants and hotels, which have coordinates
        from Geoapify but no encyclopaedia article of their own.
        """
        payload = await self._get(
            COMMONS_API,
            "commons-geosearch",
            {
                "action": "query",
                "generator": "geosearch",
                "ggsnamespace": "6",
                "ggsradius": str(GEOSEARCH_RADIUS_M),
                "ggscoord": f"{latitude}|{longitude}",
                "ggslimit": "3",
                "prop": "imageinfo",
                "iiprop": "url",
                "iiurlwidth": "800",
                "format": "json",
            },
        )
        pages = (payload.get("query") or {}).get("pages") or {}
        for page in pages.values():
            info = (page.get("imageinfo") or [{}])[0]
            url = info.get("thumburl") or info.get("url")
            if url:
                return PlaceImage(
                    url=url,
                    tier="commons_geo",
                    is_representative=False,
                    credit="Wikimedia Commons",
                    source_page=info.get("descriptionurl"),
                )
        return None

    def _keyword_fallback(self, name: str, destination: str | None, kind: str | None) -> PlaceImage:
        """A similar-looking photo when no genuine one exists.

        LoremFlickr serves Creative Commons photographs matched on keywords
        and needs no key. The lock parameter is derived from the name, so a
        given place always shows the same picture rather than changing on
        every render - a card that reshuffles its own photo looks broken.
        """
        terms = [t for t in (kind, destination or name) if t]
        keywords = ",".join(quote(_clean(t).lower().replace(" ", "")) for t in terms) or "travel"
        lock = int(hashlib.sha256(name.encode("utf-8")).hexdigest()[:6], 16) % 10_000

        return PlaceImage(
            url=f"https://loremflickr.com/800/600/{keywords}/all?lock={lock}",
            tier="loremflickr",
            is_representative=True,
            credit="Representative photo (Flickr, Creative Commons)",
        )

    # -- Plumbing ----------------------------------------------------------

    async def _get(self, url: str, service: str, params: dict[str, str]) -> dict[str, Any]:
        """One MediaWiki request, degrading to an empty result on failure.

        Deliberately swallows failures: an image is decoration on top of an
        answer that is already correct, so a Wikimedia outage must never turn
        into a failed reply.
        """
        try:
            return await request_json("GET", url, service=service, params=params)
        except ExternalServiceError:
            logger.info("images.lookup_failed", service=service)
            return {}
        except Exception:
            logger.warning("images.lookup_error", service=service, exc_info=True)
            return {}


def _clean(value: str) -> str:
    """Strip itinerary phrasing and sub-article paths from a place name.

    Wikivoyage titles arrive as paths (``Geneva/Old Town``) and model output
    arrives with narrative attached (``Visit the Old Town in the morning``).
    Neither searches well.

    Args:
        value: A raw place name.

    Returns:
        A trimmed name suitable for a search query.
    """
    text = value.split("/")[-1]
    text = re.sub(r"\(.*?\)", " ", text)
    text = _NOISE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(" ,.-") or value


def _titles_overlap(wanted: str, found: str) -> bool:
    """Whether a Wikipedia result is plausibly about the thing we searched for.

    Compares the distinctive words of each - tokens of four characters or
    more, so "cathedral" counts and "de" does not - and requires at least one
    in common. Crude, but it is the difference between "St. Pierre Cathedral"
    correctly matching `St. Pierre Cathedral` and "Cafe du Soleil" wrongly
    matching an unrelated article whose photo would then be captioned as that
    restaurant.

    Args:
        wanted: The name searched for.
        found: The article title returned.

    Returns:
        True when the two share a distinctive word.
    """

    def tokens(value: str) -> set[str]:
        return {word for word in re.findall(r"\w+", value.lower()) if len(word) >= 4}

    wanted_tokens = tokens(wanted)
    if not wanted_tokens:
        return False
    return bool(wanted_tokens & tokens(found))


def _first_thumbnail(payload: dict[str, Any]) -> str | None:
    """Pull a thumbnail URL out of a MediaWiki `pageimages` response."""
    pages = (payload.get("query") or {}).get("pages") or {}
    for page in pages.values():
        url = ((page or {}).get("thumbnail") or {}).get("source")
        if url:
            return str(url)
    return None


_resolver: ImageResolver | None = None


def get_image_resolver(settings: Settings | None = None) -> ImageResolver:
    """Return the process-wide resolver, so its cache is shared."""
    global _resolver
    if _resolver is None:
        _resolver = ImageResolver(settings)
    return _resolver
