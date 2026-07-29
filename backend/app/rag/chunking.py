"""Splitting articles into retrievable chunks.

The default approach in most RAG tutorials - slide a fixed character window
with some overlap - is a poor fit for this corpus, and the reason is worth
stating because it drove the design.

A Wikivoyage "See" section is a list of individually-described attractions,
each a self-contained entry with a name, address and description. A fixed
window cuts through the middle of those entries, producing chunks that begin
mid-address and end mid-sentence. Retrieval then returns half a temple and
half a museum, and the planner writes an itinerary citing an attraction whose
name was in the previous chunk.

So chunking here follows the document's own structure:

1. Split at section boundaries first. A section is already a coherent unit
   written by a human, and its heading is a useful retrieval filter.
2. Sections that fit inside the budget stay whole.
3. Oversized sections - "See" for a major district runs to 15,000 characters -
   are split at *entry* boundaries. Wikivoyage numbers its listings, so
   `1 Kiyomizu Temple`, `2 Yasaka Shrine` are detectable split points that
   never cut an entry in half. Paragraph breaks are the fallback.
4. Every resulting chunk keeps its section label, so a dietary query can search
   only `Eat` sections and an attractions query only `See` and `Do`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from app.rag.corpus import Article

# Roughly 512 tokens at ~4 characters per token, matching the configured
# chunk budget. Kept as characters because it is what the splitter measures
# and avoids a tokeniser dependency for a heuristic.
DEFAULT_MAX_CHARS: Final[int] = 2000

# Below this a chunk carries too little context to be useful on its own, so it
# is merged into its neighbour instead of being indexed separately.
MIN_CHUNK_CHARS: Final[int] = 200

# Wikivoyage numbers its listings: "1 Kiyomizu Temple (清水寺), 1-chome...".
# Splitting here means an attraction is never cut in half.
_LISTING_START = re.compile(r"(?m)^(?=\d{1,2}\s+\S)")


@dataclass
class Chunk:
    """One indexable passage.

    Attributes:
        document_title: Article this came from, e.g. `"Kyoto/Higashiyama"`.
        section: Section heading, used as a retrieval filter.
        content: The passage text.
        chunk_index: Position within the article, for stable ordering.
        url: Source URL, carried through so answers can cite it.
    """

    document_title: str
    section: str
    content: str
    chunk_index: int
    url: str = ""

    @property
    def citation(self) -> str:
        """A short human-readable source label."""
        return f"{self.document_title} - {self.section}"


def _split_oversized(text: str, max_chars: int) -> list[str]:
    """Split a long section at entry or paragraph boundaries.

    Args:
        text: Section text exceeding the budget.
        max_chars: Target maximum characters per piece.

    Returns:
        Pieces, each within the budget where the text allowed it. A single
        entry longer than the budget is kept intact rather than cut - an
        oversized but coherent chunk beats two incoherent ones.
    """
    # Prefer listing boundaries; fall back to blank lines for prose sections.
    parts = [part for part in _LISTING_START.split(text) if part.strip()]
    if len(parts) <= 1:
        parts = [part for part in re.split(r"\n\s*\n", text) if part.strip()]
    if len(parts) <= 1:
        parts = [part for part in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text) if part.strip()]

    pieces: list[str] = []
    buffer: list[str] = []
    length = 0

    for part in parts:
        part_length = len(part)
        if buffer and length + part_length > max_chars:
            pieces.append("\n".join(buffer).strip())
            buffer, length = [], 0
        buffer.append(part.strip())
        length += part_length

    if buffer:
        pieces.append("\n".join(buffer).strip())

    return [piece for piece in pieces if piece]


def chunk_article(article: Article, *, max_chars: int = DEFAULT_MAX_CHARS) -> list[Chunk]:
    """Split an article into retrievable chunks.

    Args:
        article: The parsed article.
        max_chars: Target maximum characters per chunk.

    Returns:
        Chunks in document order. Empty if the article had no usable prose.

    Example:
        >>> chunks = chunk_article(article)
        >>> {chunk.section for chunk in chunks}
        {'Summary', 'See', 'Do', 'Eat', 'Sleep'}
    """
    chunks: list[Chunk] = []
    index = 0
    pending: Chunk | None = None

    for section, text in article.sections:
        cleaned = text.strip()
        if not cleaned:
            continue

        pieces = [cleaned] if len(cleaned) <= max_chars else _split_oversized(cleaned, max_chars)

        for piece in pieces:
            # Merge a runt into the previous chunk when they share a section,
            # so a two-line "Drink" section does not become its own near-empty
            # index entry.
            if (
                pending is not None
                and len(piece) < MIN_CHUNK_CHARS
                and pending.section == section
                and len(pending.content) + len(piece) <= max_chars * 1.4
            ):
                pending.content = f"{pending.content}\n{piece}"
                continue

            if pending is not None:
                chunks.append(pending)

            pending = Chunk(
                document_title=article.title,
                section=section,
                content=piece,
                chunk_index=index,
                url=article.url,
            )
            index += 1

    if pending is not None:
        chunks.append(pending)

    # Prefix each chunk with its provenance. Embedding the heading alongside
    # the body measurably helps: "Eat" and "Sleep" sections of the same
    # district otherwise embed very close together, since they share place
    # names, street names and tone.
    for chunk in chunks:
        chunk.content = f"[{chunk.document_title} - {chunk.section}]\n{chunk.content}"

    return chunks
