"""Per-process CSRF token (UI-3)."""

import hmac
import secrets

from starlette.requests import Request

STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
HEADER_NAME = "X-CSRF-Token"
FORM_FIELD = "csrf_token"
_FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


class CsrfProtection:
    """One random token per instance; state-changing requests must present it (UI-3)."""

    def __init__(self) -> None:
        self.token = secrets.token_urlsafe(32)

    def _matches(self, candidate: object) -> bool:
        if not isinstance(candidate, str):
            return False
        return hmac.compare_digest(candidate.encode("utf-8"), self.token.encode("utf-8"))

    async def check(self, request: Request) -> bool:
        """True when the request is not state-changing or carries the token as header or form field."""
        if request.method not in STATE_CHANGING_METHODS:
            return True
        if self._matches(request.headers.get(HEADER_NAME)):
            return True
        content_type = request.headers.get("content-type", "").lower()
        if not content_type.startswith(_FORM_TYPES):
            return False
        try:
            form = await request.form()
        except Exception:  # a body that does not parse carries no valid token
            return False
        try:
            return self._matches(form.get(FORM_FIELD))
        finally:
            await form.close()
