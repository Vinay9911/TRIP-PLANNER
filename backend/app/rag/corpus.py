"""Wikivoyage corpus client.

Wikivoyage is the retrieval corpus for this project, and the choice is
structural rather than convenient. Three properties matter:

**Districts are addressable.** Large cities publish their neighbourhoods as
subpages - `Tokyo/Shinjuku`, `Kyoto/Higashiyama`, `Paris/11th`. That means
"which districts does this city have?" is a real API query, not an LLM guess.
This is the hinge the multi-hop retrieval turns on: hop 1 enumerates the
districts, hop 2 retrieves the specific ones that match the traveller, and the
link between them is a fact rather than a hallucination.

**Sections are consistent.** Every article uses the same headings - Understand,
Get in, See, Do, Buy, Eat, Drink, Sleep. That gives retrieval a free metadata
filter: a dietary question searches `Eat`, an attractions question searches
`See` and `Do`. Verified against the live API rather than assumed.

**It is genuinely free.** No key, no quota, CC BY-SA licensed. The only
requirement is Wikimedia's robot policy, which mandates a User-Agent
identifying the client and a contact address - requests without one get a 403.

Articles are fetched on demand and cached with a TTL rather than pre-ingested.
A full dump is tens of gigabytes against a 500 MB free-tier database; fetching
on demand means the index warms around the cities people actually ask about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.services.http import request_json

logger = get_logger(__name__)

# The sections worth retrieving, mapped to what a planner would want them for.
# Anything not listed here (Get in, Connect, Stay safe...) is still fetched but
# ranks lower, because it rarely answers an itinerary question.
SECTION_PURPOSE: Final[dict[str, str]] = {
    "Understand": "background, character, orientation",
    "See": "sights and attractions",
    "Do": "activities and experiences",
    "Eat": "restaurants and food",
    "Drink": "bars, cafes and nightlife",
    "Buy": "shopping and markets",
    "Sleep": "accommodation and which area to stay in",
    "Get around": "local transport",
}

# Sections that are almost never useful in an itinerary and add noise plus
# embedding cost if indexed.
SKIPPED_SECTIONS: Final[frozenset[str]] = frozenset(
    {"References", "External links", "See also", "Cope", "Connect", "Go next"}
)

MAX_DISTRICTS: Final[int] = 60

# Headings under which a region-level article (a country, a state, a large
# area with no district subpages) lists its own children. Checked in order;
# the first one present is used.
_CHILD_DESTINATION_HEADINGS: Final[tuple[str, ...]] = (
    "Cities",
    "Regions",
    "Districts",
    "Other destinations",
)

# A Wikivoyage numbered listing entry: "1 Kalpetta - capital of Wayanad...".
# Matches the leading number and captures the name up to the first dash,
# en-dash or opening parenthesis - the point where the description starts.
# Curly apostrophe and en-dash are built from chr() rather than pasted
# literally, since both are visually indistinguishable from their ASCII
# counterparts in most editors and a future reader could not otherwise tell
# which one a given character actually is.
_NAME_PUNCTUATION = "'" + chr(0x2019) + ".-"
_DASH_CLASS = "(" + chr(0x2013) + "-"
_CHILD_LISTING = re.compile(
    rf"^\d{{1,2}}\s+([A-Z][\w{_NAME_PUNCTUATION}]*(?:\s+[A-Z][\w{_NAME_PUNCTUATION}]*){{0,3}})"
    rf"(?:\s*[{_DASH_CLASS}]|\s*$)",
    re.MULTILINE,
)


@dataclass
class Article:
    """A fetched Wikivoyage article.

    Attributes:
        title: Exact page title, e.g. `"Tokyo/Shinjuku"`.
        url: Human-readable URL, used for citation.
        sections: Ordered `(heading, text)` pairs.
        districts: Sub-article titles, populated for city articles.
    """

    title: str
    url: str
    sections: list[tuple[str, str]] = field(default_factory=list)
    districts: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when the article had no usable prose."""
        return not self.sections

    def section_text(self, heading: str) -> str:
        """Return the text of one section, or an empty string.

        Args:
            heading: Section heading to look for, matched case-insensitively.

        Returns:
            The section's text.
        """
        target = heading.strip().lower()
        for name, text in self.sections:
            if name.strip().lower() == target:
                return text
        return ""


def _article_url(title: str) -> str:
    """Build the public URL for an article title."""
    return f"https://en.wikivoyage.org/wiki/{title.replace(' ', '_')}"


def parse_sections(extract: str) -> list[tuple[str, str]]:
    """Split a plain-text MediaWiki extract into `(heading, text)` pairs.

    The API's `explaintext` output keeps wiki heading markers (`== See ==`,
    `=== By train ===`). Subsections are folded into their parent, because a
    Wikivoyage subsection like "By train" is a fragment that means little
    without "Get in" around it, and indexing fragments produces retrieval hits
    that cannot be understood on their own.

    Args:
        extract: Plain-text article body from the MediaWiki API.

    Returns:
        Ordered `(heading, text)` pairs. Lead prose before the first heading
        is returned under the heading `"Summary"`.
    """
    if not extract.strip():
        return []

    heading_pattern = re.compile(r"^(={2,6})\s*(.+?)\s*\1\s*$", re.MULTILINE)

    sections: list[tuple[str, str]] = []
    current_heading = "Summary"
    current_level = 2
    buffer: list[str] = []

    position = 0
    for match in heading_pattern.finditer(extract):
        buffer.append(extract[position : match.start()])
        position = match.end()

        level = len(match.group(1))
        heading = match.group(2).strip()

        if level > current_level:
            # A subsection: keep its text with the parent, but leave the
            # heading inline so the context is not lost entirely.
            buffer.append(f"\n{heading}: ")
            continue

        text = "".join(buffer).strip()
        if text:
            sections.append((current_heading, text))

        buffer = []
        current_heading = heading
        current_level = level

    buffer.append(extract[position:])
    trailing = "".join(buffer).strip()
    if trailing:
        sections.append((current_heading, trailing))

    # Stub sections - a heading with a fragment under it - carry no
    # retrievable meaning and only dilute the index. The floor is set at
    # roughly one short sentence; anything longer is kept, and `chunk_article`
    # merges small pieces into their neighbours anyway.
    return [
        (heading, text)
        for heading, text in sections
        if heading not in SKIPPED_SECTIONS and len(text) >= 25
    ]


def extract_child_destinations(sections: list[tuple[str, str]]) -> list[str]:
    """Pull candidate place names from a region article's own listings.

    For a city like Kyoto, districts are addressable subpages and
    `list_districts` finds them directly. A state or country article - Kerala,
    say - has no such subpages, but its `Cities` or `Regions` section is a
    numbered list of real places in exactly the format Wikivoyage uses for
    every listing: `1 Kalpetta - capital of Wayanad district...`.

    This extracts the leading name from each entry. The names are candidates,
    not confirmed articles - `WikivoyageClient.resolve_child_destinations`
    validates each one by attempting to resolve it, the same way any
    destination name is resolved, so a name that turns out not to have its own
    article costs nothing rather than producing a failed fetch downstream.

    Args:
        sections: Parsed `(heading, text)` pairs from `parse_sections`.

    Returns:
        Candidate names in the order they appeared, deduplicated.
    """
    by_heading = dict(sections)

    # Iterate the priority list, not the article's own section order. Kerala's
    # article happens to put "Regions" before "Cities", and the first
    # implementation of this function returned whichever heading it met
    # first while walking the article - silently preferring the broader,
    # less useful Regions listing whenever it came first, which a test
    # constructed the other way around than the live article caught
    # immediately.
    for heading in _CHILD_DESTINATION_HEADINGS:
        text = by_heading.get(heading)
        if text is None:
            continue

        candidates: list[str] = []
        seen: set[str] = set()
        for match in _CHILD_LISTING.finditer(text):
            name = match.group(1).strip()
            key = name.lower()
            if name and key not in seen:
                seen.add(key)
                candidates.append(name)

        if candidates:
            return candidates[:MAX_DISTRICTS]

    return []


class WikivoyageClient:
    """Fetches articles and district listings from Wikivoyage.

    Attributes:
        settings: Application settings supplying the API base URL.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        """Initialise the client.

        Args:
            settings: Settings override, for tests.
        """
        self.settings = settings or get_settings()

    async def _query(self, params: dict[str, Any]) -> dict[str, Any]:
        """Issue a MediaWiki API request.

        Args:
            params: Query parameters, merged with `format=json`.

        Returns:
            The decoded response.

        Raises:
            ExternalServiceError: If the API is unreachable or errors.
        """
        return await request_json(
            "GET",
            self.settings.wikivoyage_api_base_url,
            service="wikivoyage",
            params={**params, "format": "json", "formatversion": "1"},
        )

    async def find_article_title(self, place: str) -> str | None:
        """Resolve a place name to an exact article title.

        Uses the search endpoint rather than assuming the name matches the
        title, so "kyoto japan" and "tokyo" both resolve.

        Args:
            place: A place name.

        Returns:
            The best-matching article title, or None if nothing matched.

        Raises:
            ExternalServiceError: If the API is unreachable.
        """
        payload = await self._query(
            {
                "action": "query",
                "list": "search",
                "srsearch": place,
                "srlimit": 3,
                "srnamespace": 0,
            }
        )
        results = payload.get("query", {}).get("search", [])
        if not results:
            return None

        # Prefer an exact case-insensitive match over the top-ranked result:
        # searching "Paris" can rank a themed travel topic above the city.
        normalised = place.strip().lower()
        for result in results:
            if result["title"].strip().lower() == normalised:
                return str(result["title"])

        return str(results[0]["title"])

    async def list_districts(self, city_title: str) -> list[str]:
        """List a city's district sub-articles.

        This is the API call that makes multi-hop retrieval real: it returns
        the actual set of neighbourhood articles that exist, so hop 2 can
        retrieve specific documents rather than guessing at names.

        Args:
            city_title: Exact city article title, e.g. `"Tokyo"`.

        Returns:
            District article titles. Empty for cities without sub-articles,
            which is normal for smaller destinations.

        Raises:
            ExternalServiceError: If the API is unreachable.
        """
        payload = await self._query(
            {
                "action": "query",
                "list": "allpages",
                "apprefix": f"{city_title}/",
                "apnamespace": 0,
                "aplimit": MAX_DISTRICTS,
            }
        )
        titles = [page["title"] for page in payload.get("query", {}).get("allpages", [])]
        return _deduplicate_districts(titles)

    async def resolve_child_destinations(self, candidates: list[str]) -> list[str]:
        """Confirm which candidate names have their own Wikivoyage article.

        `extract_child_destinations` reads names out of prose - "Kalpetta",
        "Alappuzha" - which are real places but not guaranteed to be article
        titles verbatim, or articles at all (a small town might only ever be
        mentioned inside its district's page). Each candidate is resolved the
        same way any top-level destination is, via search, so a name with no
        article of its own is dropped rather than causing a failed fetch two
        steps later.

        Args:
            candidates: Names extracted from a region article's listings.

        Returns:
            Resolved, deduplicated article titles, in the input order.
            Capped and run concurrently since this can be several lookups.
        """
        if not candidates:
            return []

        import asyncio

        # A firm cap independent of MAX_DISTRICTS: each entry costs a search
        # API call, and a region article can list dozens of "other
        # destinations" that are not worth that cost for a travel-planning
        # request that will only ever use two or three of them.
        bounded = candidates[:15]
        results = await asyncio.gather(
            *(self.find_article_title(name) for name in bounded),
            return_exceptions=True,
        )

        resolved: list[str] = []
        seen: set[str] = set()
        for name, title in zip(bounded, results, strict=True):
            if isinstance(title, BaseException) or not title:
                continue
            # A search for "Kalpetta" can resolve to an unrelated same-named
            # article elsewhere in the world; requiring the resolved title to
            # start with the candidate name is a cheap guard against that.
            if not title.lower().startswith(name.lower()[:4]):
                continue
            if title not in seen:
                seen.add(title)
                resolved.append(title)

        return resolved

    async def fetch_article(self, title: str) -> Article | None:
        """Fetch and parse one article.

        Args:
            title: Exact article title.

        Returns:
            The parsed article, or None if the page does not exist.

        Raises:
            ExternalServiceError: If the API is unreachable.
        """
        payload = await self._query(
            {
                "action": "query",
                "prop": "extracts",
                "titles": title,
                "explaintext": 1,
                "redirects": 1,
            }
        )

        pages = payload.get("query", {}).get("pages", [])
        if not pages:
            return None

        page = pages[0] if isinstance(pages, list) else next(iter(pages.values()))
        if "missing" in page or not page.get("extract"):
            logger.info("wikivoyage.article_missing", title=title)
            return None

        resolved_title = page.get("title", title)
        return Article(
            title=resolved_title,
            url=_article_url(resolved_title),
            sections=parse_sections(page["extract"]),
        )


def _deduplicate_districts(titles: list[str]) -> list[str]:
    """Collapse redirect variants of the same district.

    Wikivoyage keeps redirects as real pages, so `allpages` returns both
    `Paris/10th` and `Paris/10th arrondissement`. Indexing both wastes
    embedding quota and gives the planner two names for one place, which reads
    as a mistake in an itinerary.

    Keeps the longer, more descriptive title when two normalise identically or
    one is a prefix of the other.

    Args:
        titles: Raw district titles from the API.

    Returns:
        Deduplicated titles, sorted.
    """
    by_key: dict[str, str] = {}

    for title in sorted(titles, key=len, reverse=True):
        suffix = title.split("/", 1)[-1]
        key = re.sub(r"[^a-z0-9]", "", suffix.lower())
        if not key:
            continue

        # A shorter title whose key prefixes an already-kept longer one is the
        # redirect stub ("10th" vs "10tharrondissement"); drop it.
        if any(existing.startswith(key) for existing in by_key):
            continue

        by_key[key] = title

    return sorted(by_key.values())
