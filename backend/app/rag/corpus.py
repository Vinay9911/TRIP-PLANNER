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

    return [
        (heading, text)
        for heading, text in sections
        if heading not in SKIPPED_SECTIONS and len(text) > 40
    ]


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
