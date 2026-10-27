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
        default=DEFAULT_PORT,
        help=f"port on 127.0.0.1 (default {DEFAULT_PORT}, 0 = any free)",
    )
    serve.add_argument("--no-browser", action="store_true", help="do not open the browser")
    serve.set_defaults(func=run_serve)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


def run_serve(args: argparse.Namespace) -> int:
    port = _free_port() if args.port == 0 else args.port
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
