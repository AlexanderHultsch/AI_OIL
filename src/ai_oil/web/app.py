"""Application factory of the local web interface (UI-1, UI-2, UI-3)."""

from pathlib import Path

import sqlalchemy
from fastapi import FastAPI, Request
from starlette.responses import PlainTextResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ai_oil.config import Config
from ai_oil.errors import AiOilError, NotFoundError
from ai_oil.web.csrf import STATE_CHANGING_METHODS, CsrfProtection
from ai_oil.web.rendering import is_htmx, render
from ai_oil.web.routes import home

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


class SecurityMiddleware:
    """Rejects foreign Host headers (DNS rebinding) and state-changing requests without the token (UI-3)."""

    def __init__(self, app: ASGIApp, *, csrf: CsrfProtection, port: int) -> None:
        self.app = app
        self.csrf = csrf
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if request.headers.get("host") not in self.allowed_hosts:
            await PlainTextResponse("invalid host", status_code=400)(scope, receive, send)
            return
        if request.method in STATE_CHANGING_METHODS:
            body = await self._read_body(receive)
            if body is None:  # the client went away before the body was complete
                return
            if not await self.csrf.check(Request(scope, self._replay(body, receive))):
                await PlainTextResponse("missing or invalid CSRF token", status_code=403)(
                    scope, receive, send
                )
                return
            receive = self._replay(body, receive)
        await self.app(scope, receive, send)

    @staticmethod
    async def _read_body(receive: Receive) -> bytes | None:
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return None
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                return b"".join(chunks)

    @staticmethod
    def _replay(body: bytes, receive: Receive) -> Receive:
        """A `receive` that delivers the buffered body once, then defers to the original."""
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return replay


def _error_response(request: Request, exc: AiOilError, status_code: int) -> Response:
    template = "fragments/error.html" if is_htmx(request) else "error.html"
    return render(request, template, {"message": str(exc)}, status_code=status_code)


async def _not_found_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, NotFoundError)
    return _error_response(request, exc, 404)


async def _domain_error_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, AiOilError)
    return _error_response(request, exc, 422)


def create_app(config: Config, engine: sqlalchemy.Engine, *, port: int) -> FastAPI:
    """Build the application for the server listening on 127.0.0.1:`port`."""
    app = FastAPI(title="AI_OIL", docs_url=None, redoc_url=None, openapi_url=None)
    csrf = CsrfProtection()
    app.state.config = config
    app.state.engine = engine
    app.state.csrf = csrf
    app.state.templates = Jinja2Templates(directory=TEMPLATES_DIR)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_middleware(SecurityMiddleware, csrf=csrf, port=port)
    app.add_exception_handler(NotFoundError, _not_found_handler)
    app.add_exception_handler(AiOilError, _domain_error_handler)
    app.include_router(home.router)
    return app
