"""Swappable implementations of a single capability.

Used where the concrete backend is a deployment decision rather than an
architectural one. Flight search is the current example: a deterministic mock
by default, with a live provider selectable by configuration.
"""
