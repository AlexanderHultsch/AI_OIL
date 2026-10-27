"""Action classes and their enforcement (GOV-6, GOV-9)."""

from collections.abc import Callable
from typing import TypeVar

from ai_oil.errors import AiOilError

ACTION_CLASSES = ("A", "B", "C", "D", "E")

F = TypeVar("F", bound=Callable[..., object])


class ApprovalRequired(AiOilError):
    """A class C-E action was attempted without a valid approval (GOV-6)."""


def action(action_class: str) -> Callable[[F], F]:
    """Declare the action class of a tool; the function itself is returned unchanged (GOV-9)."""
    if action_class not in ACTION_CLASSES:
        raise ValueError(f"unknown action class: {action_class!r}")

    def decorate(func: F) -> F:
        func.__action_class__ = action_class  # type: ignore[attr-defined]
        return func

    return decorate


def action_class_of(obj: object) -> str:
    """Class of a tool or connector; anything undeclared or unknown counts as E (GOV-9)."""
    cls = getattr(obj, "__action_class__", None)
    if cls is None:
        cls = getattr(obj, "action_class", None)
    if cls not in ACTION_CLASSES:
        return "E"
    return cls


def ensure_allowed(obj: object) -> str:
    """Return the class of an action that may run without an approval token, otherwise refuse (GOV-6)."""
    cls = action_class_of(obj)
    if cls in ("A", "B"):
        return cls
    raise ApprovalRequired(
        f"class {cls} actions need an approval token, which is not available before phase 5 (GOV-6)"
    )
