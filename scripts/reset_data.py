"""Erase every row of application data, leaving the schema intact.

Written for one specific job: handing over a clean deployment. A reviewer
opening the app should find an empty history, not somebody else's half-finished
Jaipur trip and a memory profile full of test preferences.

**What it does and does not touch.** Tables are emptied; nothing is dropped.
Migrations, extensions, policies, functions and triggers all survive, so the
database is immediately usable rather than needing a re-apply. Auth users are
removed only when `--include-auth` is passed, because deleting `auth.users`
cascades into `profiles` and is the one action here that cannot be undone by
simply chatting again.

**Order matters and is not left to chance.** `truncate ... cascade` would be
shorter, but it silently follows foreign keys into tables the caller did not
name, which is precisely the behaviour you do not want from a destructive
script. The list below is ordered child-before-parent so plain `delete` works
and every deletion is one the author chose.

Run it with the same `DATABASE_URL` the application uses:

    python scripts/reset_data.py            # counts only, changes nothing
    python scripts/reset_data.py --apply    # actually erase
    python scripts/reset_data.py --apply --include-auth
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Deliberately synchronous. psycopg's async mode cannot run on Windows'
# ProactorEventLoop, which is what `asyncio.run` selects by default there - the
# same trap `backend/run.py` works around for the server. A one-shot
# maintenance script has nothing to gain from concurrency, so the simplest
# escape is not to need an event loop at all.

# Importable without installing the backend package: this is an operational
# script, and needing `pip install -e .` before you can empty a table would be
# a poor trade.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

#: Child tables first, so ordinary DELETE never trips a foreign key.
#:
#: `profiles` is deliberately last among the public tables and is emptied only
#: alongside auth: a profile row without its `auth.users` parent cannot be
#: recreated by the signup trigger, which would leave a signed-in user unable to
#: write anything and no obvious reason why.
TABLES_IN_ORDER: tuple[str, ...] = (
    # Trace detail, then the runs that own it.
    "public.tool_calls",
    "public.agent_steps",
    "public.agent_runs",
    # Conversation content, then the conversations.
    "public.message_feedback",
    "public.messages",
    "public.sessions",
    # Long-term memory.
    "public.memories",
    # Retrieval cache. Safe to clear - it refills from Wikivoyage on demand,
    # at the cost of re-embedding, so it is included for a genuinely clean
    # hand-over rather than because it holds anything private.
    "public.document_chunks",
    "public.documents",
    # Audit trail.
    "public.admin_audit_log",
)


def main() -> int:
    """Count or erase application data.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Without this the script only reports counts.",
    )
    parser.add_argument(
        "--include-auth",
        action="store_true",
        help="Also delete auth.users and profiles, removing every account.",
    )
    parser.add_argument(
        "--keep-rag-cache",
        action="store_true",
        help="Leave the Wikivoyage cache in place, so the first demo is faster.",
    )
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        # Read from .env the same way the application does, so this works with
        # no extra shell setup.
        from dotenv import dotenv_values

        root = Path(__file__).resolve().parent.parent
        for candidate in (root / ".env", root / "backend" / ".env"):
            if candidate.exists():
                url = dotenv_values(candidate).get("DATABASE_URL")
                if url:
                    break

    if not url:
        print("DATABASE_URL is not set, and no .env file supplied one.")
        return 1

    import psycopg

    tables = list(TABLES_IN_ORDER)
    if args.keep_rag_cache:
        tables = [t for t in tables if t not in ("public.document_chunks", "public.documents")]

    with psycopg.connect(url) as conn:
        print(f"{'TABLE':<32} {'ROWS':>10}")
        print("-" * 44)

        total = 0
        for table in tables:
            # The table list is a module-level constant, never user input, so
            # interpolating it is safe - and psycopg cannot parameterise an
            # identifier anyway. S608 is acknowledged here rather than
            # suppressed silently.
            cursor = conn.execute(f"select count(*) from {table}")  # noqa: S608
            count = cursor.fetchone()[0]  # type: ignore[index]
            total += count
            print(f"{table:<32} {count:>10,}")

        auth_users = 0
        if args.include_auth:
            cursor = conn.execute("select count(*) from auth.users")
            auth_users = cursor.fetchone()[0]  # type: ignore[index]
            print(f"{'auth.users':<32} {auth_users:>10,}")

        print("-" * 44)
        print(f"{'TOTAL':<32} {total + auth_users:>10,}")

        if not args.apply:
            print("\nDry run - nothing was changed. Re-run with --apply to erase.")
            return 0

        for table in tables:
            conn.execute(f"delete from {table}")  # noqa: S608
            print(f"cleared {table}")

        if args.include_auth:
            # Cascades into public.profiles through the foreign key declared in
            # migration 0001, so profiles needs no separate delete.
            conn.execute("delete from auth.users")
            print("cleared auth.users (profiles cascade)")

        conn.commit()
        print("\nDone. The schema, policies and migrations are untouched.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
