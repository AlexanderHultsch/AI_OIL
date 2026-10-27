"""Template rendering shared by pages and fragments."""

from typing import Any

from fastapi import Request
from starlette.responses import Response


def is_htmx(request: Request) -> bool:
    """True for requests made by htmx, which get fragments instead of full pages."""
    return request.headers.get("HX-Request") == "true"


def render(request: Request, template: str, context: dict[str, Any], *, status_code: int = 200) -> Response:
    """Render `template` with the context plus `csrf_token`, `nav_active` and `is_htmx`."""
    full_context = {"nav_active": None, **context}
    full_context["csrf_token"] = request.app.state.csrf.token
    full_context["is_htmx"] = is_htmx(request)
    return request.app.state.templates.TemplateResponse(
        request, template, full_context, status_code=status_code
    )
