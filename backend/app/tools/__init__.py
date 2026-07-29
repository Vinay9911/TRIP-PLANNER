"""The agent's action surface.

Each tool is a typed, documented function the model may choose to call. Two
rules govern this package:

1. **The model decides.** Nothing here is invoked by keyword matching or
   conditional routing. Tool choice is the model's, driven by the schema and
   docstring of each tool - which is why those docstrings are written for the
   model as much as for a human reader.

2. **A failing tool degrades, it does not raise.** Every tool catches upstream
   failures and returns a structured result saying the source was
   unavailable, so a weather API outage costs the agent one fact rather than
   the entire run.
"""
