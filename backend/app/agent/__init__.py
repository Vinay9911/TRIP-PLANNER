"""The planning agent.

A hand-built LangGraph `StateGraph` implementing clarify -> plan -> execute ->
replan -> respond. LangChain 1.x removed the prebuilt Plan-and-Execute agent
named in the assignment; `create_agent` replaced it but is a ReAct loop, not a
planner. So the planning loop is explicit here, and `create_agent` is used
*inside* the executor node for the part it does well: tool calling.
"""
