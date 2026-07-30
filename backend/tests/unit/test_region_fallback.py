"""Tests for the region-article fallback found by reviewing a live Kerala run.

Wikivoyage's `City/District` subpage convention, which the multi-hop retriever
was built and tested against, holds for cities (Kyoto, Tokyo, Paris) but not
for states or countries. A real request about Kerala showed `list_districts`
correctly returning nothing - Kerala has no `Kerala/Wayanad` subpage - and the
retriever silently falling back to a single flat search, while the *executor
agent* compensated by manually calling `search_travel_guide` several times
with place names (Wayanad, Idukki) it already knew from its own training.

That produces a reasonable-looking answer, but it is not the chained
"discover real children, then investigate them" retrieval the rest of this
project's RAG claim rests on - the model was doing the discovery, not the
retriever. `extract_child_destinations` and `resolve_child_destinations` close
that gap by reading the region article's own `Cities`/`Regions` listing, which
Wikivoyage already writes in a fixed, parseable format.
"""

from __future__ import annotations

from app.rag.corpus import extract_child_destinations


def test_extracts_names_from_a_numbered_cities_listing():
    """The exact format found in the live Kerala article."""
    sections = [
        (
            "Cities",
            "Here are nine of the most notable cities.\n"
            "1 Thiruvananthapuram (Trivandrum) - the capital city, famous for beaches\n"
            "2 Alappuzha (Alleppey) - heartland of Kerala Backwaters\n"
            "3 Kalpetta - capital of Wayanad district, home to wildlife sanctuaries\n"
            "4 Kannur (Cannanore) - a historical town famous for martial arts\n",
        ),
    ]

    names = extract_child_destinations(sections)

    assert names == ["Thiruvananthapuram", "Alappuzha", "Kalpetta", "Kannur"]


def test_prefers_cities_over_regions_when_both_present():
    """Cities are the more useful, more specific level to research first."""
    sections = [
        ("Regions", "1 Northern Kerala - hills and forests\n2 Southern Kerala - backwaters\n"),
        ("Cities", "1 Kochi - the commercial capital\n2 Kannur - a historical town\n"),
    ]

    names = extract_child_destinations(sections)

    assert names == ["Kochi", "Kannur"]


def test_falls_back_to_regions_when_no_cities_section_exists():
    sections = [
        ("Understand", "A large country with diverse regions."),
        ("Regions", "1 Northern Province - mountains\n2 Southern Province - coast\n"),
    ]

    names = extract_child_destinations(sections)

    assert names == ["Northern Province", "Southern Province"]


def test_returns_nothing_for_a_normal_city_article():
    """A city like Kyoto has no Cities/Regions listing - the fallback must be inert."""
    sections = [
        ("Understand", "Kyoto was the imperial capital of Japan."),
        ("See", "1 Kiyomizu Temple - a famous wooden temple.\n"),
    ]

    assert extract_child_destinations(sections) == []


def test_ignores_lowercase_and_malformed_entries():
    """Only real numbered listings with a capitalised name should match."""
    sections = [
        (
            "Cities",
            "some intro prose with no numbers\n"
            "1 lowercase entry should not match\n"
            "2 Valid City - a real place\n",
        ),
    ]

    names = extract_child_destinations(sections)

    assert names == ["Valid City"]


def test_deduplicates_repeated_names():
    sections = [("Cities", "1 Kochi - the port\n2 Kochi - repeated by mistake\n")]

    assert extract_child_destinations(sections) == ["Kochi"]
