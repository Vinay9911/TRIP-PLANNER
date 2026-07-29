"""Multi-hop retrieval over travel guides.

Retrieval chains rather than fires once: each hop's query is built from the
previous hop's results. The corpus is Wikivoyage, chosen because its article
structure (city -> district sub-articles -> consistent See/Eat/Sleep
sections) supplies a real link between hops instead of a synthetic one.
"""
