"""Regression tests for a bug chain found by inspecting a real production run.

Reading the admin trace for an actual Kerala itinerary showed every tool call
recorded `arguments: {}`. Tracing that back:

1. `resilient_tool`'s wrapper records `**kwargs` only, but every registry
   wrapper called the underlying implementation *positionally* - so the
   arguments always arrived as `*args` and the trace recorded nothing.
2. The same positional calls broke `cache_on`: `_cache_key`'s `cache_on`
   branch was documented in two docstrings and accepted as a parameter, but
   the function body never actually implemented it - it always fell through
   to keying on the full argument set, which includes free-text fields a
   model rewords on every call. So the cache built specifically to stop
   repeated near-identical retrievals had a 0% hit rate in production despite
   looking, from the code, like it should have worked.

Both are fixed by (a) calling every tool with keywords in `registry.py` and
(b) actually implementing the `cache_on` branch. These tests pin both, and are
written to fail again immediately if either regresses - a positional call is
exactly what silently broke this the first time.
"""

from __future__ import annotations

from app.tools.base import ToolResult, resilient_tool, start_recording, stop_recording


async def test_keyword_calls_are_recorded_with_their_arguments(tool_records):
    @resilient_tool(source="test", unavailable_message="x")
    async def probe(destination: str, question: str) -> ToolResult:
        return ToolResult.ok(source="test", data={"destination": destination})

    await probe(destination="Kerala", question="temples")

    assert tool_records[0].arguments == {"destination": "Kerala", "question": "temples"}


async def test_positional_calls_are_ALSO_recorded_with_their_arguments(tool_records):
    """The actual bug: every registry.py wrapper called positionally.

    If this regresses, the admin trace's `arguments` column - the direct
    evidence that tool selection is dynamic, not scripted - goes back to
    being empty for every real agent-driven call, exactly as it was in
    production.
    """

    @resilient_tool(source="test", unavailable_message="x")
    async def probe(destination: str, question: str) -> ToolResult:
        return ToolResult.ok(source="test", data={"destination": destination})

    await probe("Kerala", "temples")  # positional - how registry.py called it

    assert tool_records[0].arguments == {"destination": "Kerala", "question": "temples"}, (
        "positional arguments were not captured in the trace"
    )


async def test_registry_calls_every_underlying_tool_with_keywords():
    """Structural guard against the exact bug that caused the empty-args issue.

    Rather than re-deriving this from a live agent run, assert directly on the
    source: every `await <module>.<tool>(...)` call in registry.py must use
    keyword arguments. A positional call here silently breaks both trace
    recording and result caching, and nothing else would catch it - the
    function still returns the right *value*, just with the wrong provenance.
    """
    import ast
    import pathlib

    source_path = pathlib.Path(__file__).resolve().parents[2] / "app" / "tools" / "registry.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Only calls that reach into another tool module, e.g. `weather.get_weather_forecast(...)`.
        if not (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)):
            continue
        module_names = {
            "weather",
            "places",
            "travel_guide",
            "web_search",
            "travel_logistics",
            "memory_tools",
        }
        if node.func.value.id not in module_names:
            continue
        if node.args:  # positional arguments present
            offenders.append(f"{node.func.value.id}.{node.func.attr}")

    assert not offenders, f"positional calls found in registry.py: {offenders}"


async def test_cache_on_actually_narrows_the_key(tool_records):
    """The bug: `cache_on` was accepted and threaded through, never applied.

    A tool declaring `cache_on=("destination",)` must treat two calls with the
    same destination but a differently-worded free-text field as the same
    call, and must NOT collide two calls with genuinely different destinations.
    """
    calls = {"n": 0}

    @resilient_tool(source="test", unavailable_message="x", cache_on=("destination",))
    async def guide(destination: str, question: str) -> ToolResult:
        calls["n"] += 1
        return ToolResult.ok(source="test", data={"destination": destination, "call": calls["n"]})

    first = await guide(destination="Kerala", question="temples")
    second = await guide(destination="Kerala", question="a completely different phrasing")
    third = await guide(destination="Wayanad", question="temples")

    assert first.cached is False
    assert second.cached is True, "reworded question should have hit the cache"
    assert second.data["call"] == first.data["call"], "cache returned a stale result, not itself"
    assert third.cached is False, "a different destination must not collide with Kerala's key"
    assert calls["n"] == 2, "the underlying function ran a third time despite the cache"


async def test_tools_without_cache_on_still_match_on_full_arguments(tool_records):
    """The pre-existing fallback behaviour must survive the fix."""
    calls = {"n": 0}

    @resilient_tool(source="test", unavailable_message="x")
    async def plain(city: str) -> ToolResult:
        calls["n"] += 1
        return ToolResult.ok(source="test", data={"city": city})

    await plain(city="Kyoto")
    await plain(city="Kyoto")
    await plain(city="Osaka")

    assert calls["n"] == 2, "same full arguments should still cache without cache_on"


async def test_cache_on_isolates_different_tools_from_each_other():
    """Two tools sharing a candidate destination string must not share a cache slot."""

    @resilient_tool(source="a", unavailable_message="x", cache_on=("destination",))
    async def tool_a(destination: str) -> ToolResult:
        return ToolResult.ok(source="a", data={"from": "a"})

    @resilient_tool(source="b", unavailable_message="x", cache_on=("destination",))
    async def tool_b(destination: str) -> ToolResult:
        return ToolResult.ok(source="b", data={"from": "b"})

    start_recording()
    try:
        result_a = await tool_a(destination="Kerala")
        result_b = await tool_b(destination="Kerala")
    finally:
        stop_recording()

    assert result_a.data == {"from": "a"}
    assert result_b.data == {"from": "b"}, "tool_b served tool_a's cached result"
