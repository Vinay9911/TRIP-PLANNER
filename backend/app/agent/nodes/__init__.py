"""Graph nodes.

One module per node, each exposing a single async function that takes the
graph state and returns a partial update. Keeping them separate means a node
can be unit-tested by calling it with a hand-built state dict, with no graph
and no LLM involved.
"""
