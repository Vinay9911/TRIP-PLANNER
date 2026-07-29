"""Clients for systems outside this process.

Each module wraps one external dependency and is responsible for translating
that dependency's failure modes into the application's error taxonomy, so
that callers never have to catch a provider-specific exception type.
"""
