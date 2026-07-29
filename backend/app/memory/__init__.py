"""Two distinct memory systems.

**Short-term** (`short_term.py`) is the running conversation inside one
session, persisted through the LangGraph checkpointer and trimmed or
summarised when it outgrows the context window.

**Long-term** (`extractor.py`, `consolidator.py`, `store.py`) is durable
knowledge about a traveller: an extract, deduplicate, reconcile, embed, store
and retrieve pipeline that is deliberately decoupled from any single session.
A stored transcript is not long-term memory - you cannot ask a transcript
whether someone is vegetarian without re-reading all of it.
"""
