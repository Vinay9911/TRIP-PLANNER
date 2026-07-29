"""Development entrypoint.

Exists for one reason: **psycopg's async mode cannot run on Windows' default
event loop.**

Since Python 3.8, Windows defaults to `ProactorEventLoop`, and psycopg raises

    InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in
    async mode.

the moment it opens an async connection. So on Windows every database call
fails, the app falls back to in-memory stores, and `/health/ready` reports the
database as unconfigured - which looks like a credentials problem and is not.

Setting the event loop *policy* does not help, which is the trap. uvicorn
selects its loop through a factory:

    def asyncio_loop_factory(use_subprocess: bool = False):
        if sys.platform == "win32" and not use_subprocess:
            return asyncio.ProactorEventLoop
        return asyncio.SelectorEventLoop

so it constructs `ProactorEventLoop` directly and never consults the policy.
The only reliable fix is to build the loop ourselves and hand uvicorn a server
to run on it - which is what `main` does below.

Linux and macOS are unaffected: they get `SelectorEventLoop` already, and the
Docker image invokes `uvicorn` directly. This file is a local-development
convenience, not part of the deployed path.

Usage:

    python run.py                 # http://localhost:8000, with reload
    python run.py --port 8080
    python run.py --no-reload
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def main() -> None:
    """Parse arguments and start the development server."""
    parser = argparse.ArgumentParser(description="Run the Trip Planner API locally.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--no-reload", action="store_true", help="Disable auto-reload on file changes."
    )
    parser.add_argument("--log-level", default="info")
    arguments = parser.parse_args()

    import uvicorn

    reload_enabled = not arguments.no_reload

    # The reload supervisor runs the app in a subprocess, and uvicorn's factory
    # already returns SelectorEventLoop for that path - so reload needs no
    # special handling and we let uvicorn drive.
    if reload_enabled or sys.platform != "win32":
        uvicorn.run(
            "app.main:app",
            host=arguments.host,
            port=arguments.port,
            reload=reload_enabled,
            log_level=arguments.log_level,
        )
        return

    print("[run] Windows, no reload - running on an explicit SelectorEventLoop")

    config = uvicorn.Config(
        "app.main:app",
        host=arguments.host,
        port=arguments.port,
        log_level=arguments.log_level,
    )
    server = uvicorn.Server(config)

    # `loop_factory` is the supported way to pin the loop type (Python 3.12+)
    # and avoids the deprecated set_event_loop_policy dance.
    asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)


if __name__ == "__main__":
    main()
