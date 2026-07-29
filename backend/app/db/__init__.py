"""Database access.

`session.py` owns the connection pool and the row-level-security scoping that
constrains queries to a single user. `repositories.py` holds the SQL; no
other package writes queries directly.
"""
