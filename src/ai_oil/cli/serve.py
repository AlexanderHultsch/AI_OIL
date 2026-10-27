"""`oil serve`: the local web interface on 127.0.0.1 only (UI-2)."""

import argparse
import socket
import threading
import webbrowser

import uvicorn

from ai_oil.cli.context import open_context
from ai_oil.web.app import create_app

HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def register(subparsers) -> None:
    serve = subparsers.add_parser("serve", help="start the local web interface")
    serve.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            f"port on 127.0.0.1 (default {DEFAULT_PORT}, or a free one if {DEFAULT_PORT} is busy; "
            "0 = any free)"
        ),
    )
    serve.add_argument("--no-browser", action="store_true", help="do not open the browser")
    serve.set_defaults(func=run_serve)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((HOST, port))
        except OSError:
            return False
    return True


def _choose_port(requested: int | None) -> int:
    """0 gives any free port, an explicit number is used as is, None prefers the default port."""
    if requested == 0:
        return _free_port()
    if requested is not None:
        return requested
    if _port_is_free(DEFAULT_PORT):
        return DEFAULT_PORT
    port = _free_port()
    print(
        f"Port {DEFAULT_PORT} is busy (is AI_OIL already running? "
        f"Then use the browser tab that is already open) — using port {port} instead"
    )
    return port


def run_serve(args: argparse.Namespace) -> int:
    port = _choose_port(args.port)
    ctx = open_context(args)
    try:
        app = create_app(ctx.config, ctx.engine, port=port)
        url = f"http://{HOST}:{port}/"
        print(f"AI_OIL is running at {url} — press Ctrl+C to stop")
        if not args.no_browser:
            threading.Timer(1.0, webbrowser.open, args=(url,)).start()
        uvicorn.run(app, host=HOST, port=port, log_level="warning")
    finally:
        ctx.engine.dispose()
    return 0
