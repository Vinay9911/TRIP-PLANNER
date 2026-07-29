"""Cross-cutting concerns: configuration, logging and errors.

Everything here is importable from anywhere in the application and imports
nothing from it. If a module in `core/` ever needs something from `services/`
or `agent/`, that is a sign the abstraction belongs elsewhere.
"""
