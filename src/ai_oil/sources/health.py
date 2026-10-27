"""Connector configuration and live checks, shared by `oil connector check` and the web page (CON-6)."""

from dataclasses import dataclass

from ai_oil import secrets
from ai_oil.sources import registry


@dataclass(frozen=True)
class CheckLine:
    """One line of a check report: `[ok]` or `[FAIL]` and its text."""

    ok: bool
    text: str

    def format(self) -> str:
        return f"  [{'ok' if self.ok else 'FAIL'}] {self.text}"


def credential_state(row: registry.ConnectorRow) -> str:
    """`-` without a credential reference, else `stored` or `missing`; never the secret itself (GOV-8)."""
    if row.credential_ref is None:
        return "-"
    return "stored" if secrets.get_secret(row.credential_ref) is not None else "missing"


def check_configuration(row: registry.ConnectorRow) -> tuple[object | None, list[CheckLine]]:
    """The plugin instance (None if it cannot start) and the configuration check lines (CON-6)."""
    lines: list[CheckLine] = []
    try:
        connector: object | None = registry.instantiate(row)
    except Exception as exc:
        connector = None
        lines.append(CheckLine(False, f"plugin: {type(exc).__name__}: {exc}"))
    else:
        lines.append(CheckLine(True, "plugin loaded"))
    if row.credential_ref is not None:
        if secrets.get_secret(row.credential_ref) is not None:
            lines.append(CheckLine(True, "credential stored"))
        else:
            lines.append(CheckLine(False, f"credential missing for {row.credential_ref}"))
    return connector, lines


def check_live(connector: object) -> list[CheckLine]:
    """The read-only check against the real system (CON-6, class A)."""
    try:
        report = connector.check()  # type: ignore[attr-defined]
    except Exception as exc:
        return [CheckLine(False, f"live check: {type(exc).__name__}: {exc}")]
    return [CheckLine(check.ok, f"{check.name}: {check.detail}") for check in report.checks]
