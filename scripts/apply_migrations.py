"""Apply the SQL migrations to a Supabase project.

An alternative to pasting each file into the Supabase SQL editor by hand. The
migrations are written to be idempotent (`create or replace`, `if not exists`,
`on conflict do nothing`), so re-running is safe.

    python scripts/apply_migrations.py

Reads `DATABASE_URL` from `.env` in the repository root. Applies files in
filename order, then prints what landed so you can see it worked rather than
trusting that it did.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"


def database_url() -> str:
    """Read DATABASE_URL from the root `.env`."""
    path = ROOT / ".env"
    if not path.exists():
        print(f"No .env at {path}. Copy .env.example and fill it in.")
        sys.exit(1)

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()

    print("DATABASE_URL is not set in .env")
    sys.exit(1)


def main() -> None:
    """Apply every migration, then summarise the resulting schema."""
    try:
        import psycopg
    except ImportError:
        print("psycopg is not installed. Run this from the backend virtualenv:")
        print("  backend/.venv/Scripts/python.exe scripts/apply_migrations.py")
        sys.exit(1)

    url = database_url()
    if ":6543" in url:
        print("DATABASE_URL points at the transaction pooler (6543).")
        print(
            "Use the session pooler on 5432 - the checkpointer needs prepared statements."
        )
        sys.exit(1)

    files = sorted(MIGRATIONS.glob("*.sql"))
    if not files:
        print(f"No .sql files found in {MIGRATIONS}")
        sys.exit(1)

    print(f"Applying {len(files)} migrations\n")
    failures = 0

    # autocommit so each migration lands independently; a failure part-way
    # leaves the successful ones applied, which is what you want when
    # re-running after fixing one file.
    with psycopg.connect(url, connect_timeout=30, autocommit=True) as conn:
        for path in files:
            try:
                conn.execute(path.read_text(encoding="utf-8"))
                print(f"  [ OK ] {path.name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"  [FAIL] {path.name}\n         {str(exc)[:400]}\n")

        print(f"\n{len(files) - failures}/{len(files)} applied\n")

        tables = conn.execute(
            "select table_name from information_schema.tables "
            "where table_schema = 'public' order by table_name"
        ).fetchall()
        print(f"Tables ({len(tables)}): {', '.join(name for (name,) in tables)}")

        policies = conn.execute(
            "select count(*) from pg_policies where schemaname = 'public'"
        ).fetchone()[0]
        print(f"Row-level-security policies: {policies}")

        vector = conn.execute(
            "select extversion from pg_extension where extname = 'vector'"
        ).fetchone()
        print(f"pgvector: {vector[0] if vector else 'NOT INSTALLED'}")

    if failures:
        sys.exit(1)

    print("\nNext: create an account through the app, then make it an admin with")
    print("  select public.promote_to_admin('you@example.com');")


if __name__ == "__main__":
    main()
