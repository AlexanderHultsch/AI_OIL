"""Connector plugins (entry point group ai_oil.connectors) and configured connector instances (DM-40, DM-41, CON-4)."""  # noqa: E501

import importlib.metadata
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import sqlalchemy
from sqlalchemy import text

from ai_oil.errors import ConflictError, NotFoundError, ValidationError
from ai_oil.sources.model import ConnectorConfig, SourceRef

ENTRY_POINT_GROUP = "ai_oil.connectors"
_COLUMNS = "id, type, name, enabled, settings_json, credential_ref, created_at"
_SECRET_KEY_PARTS = ("token", "password", "secret")

_registered: dict[str, type] = {}
load_errors: dict[str, str] = {}


@dataclass(frozen=True)
class ConnectorRow:
    id: int
    type: str
    name: str
    enabled: bool
    settings: dict
    credential_ref: str | None
    created_at: str


def register_connector_type(cls: type) -> type:
    """Register a connector class in-process; it takes precedence over entry points. Usable as decorator."""
    _registered[cls.type] = cls  # type: ignore[attr-defined]
    return cls


def unregister_connector_type(type_name: str) -> None:
    _registered.pop(type_name, None)


def available_connector_types() -> dict[str, type]:
    """Connector classes from the entry point group, overlaid by in-process registrations.

    An entry point that fails to load is skipped and recorded in `load_errors` (CON-3).
    """
    load_errors.clear()
    found: dict[str, type] = {}
    for entry_point in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            found[entry_point.name] = entry_point.load()
        except Exception as exc:
            load_errors[entry_point.name] = f"{type(exc).__name__}: {exc}"
    found.update(_registered)
    return found


def get_connector_class(type_name: str) -> type:
    available = available_connector_types()
    if type_name not in available:
        listing = ", ".join(sorted(available)) or "none"
        raise NotFoundError(f"unknown connector type: {type_name} (available: {listing})")
    return available[type_name]


def _row(row: sqlalchemy.RowMapping) -> ConnectorRow:
    return ConnectorRow(
        id=row["id"],
        type=row["type"],
        name=row["name"],
        enabled=bool(row["enabled"]),
        settings=json.loads(row["settings_json"]),
        credential_ref=row["credential_ref"],
        created_at=row["created_at"],
    )


def to_config(row: ConnectorRow) -> ConnectorConfig:
    return ConnectorConfig(
        id=row.id,
        type=row.type,
        name=row.name,
        enabled=row.enabled,
        settings=row.settings,
        credential_ref=row.credential_ref,
    )


def _settings_json(settings: Mapping | None) -> str:
    settings = dict(settings or {})
    for key in settings:
        lowered = str(key).lower()
        if any(part in lowered for part in _SECRET_KEY_PARTS):
            raise ValidationError(
                f"settings key {key!r} looks like a secret: secrets belong in the Credential Manager,"
                " referenced through credential_ref (GOV-8)"
            )
    return json.dumps(settings, sort_keys=True)


def create_connector(
    conn: sqlalchemy.Connection,
    type_name: str,
    name: str,
    *,
    settings: Mapping | None = None,
    credential_ref: str | None = None,
) -> ConnectorRow:
    """Configure a connector instance of an available plugin type (DM-40, DM-41)."""
    get_connector_class(type_name)
    name = name.strip()
    if not name:
        raise ValidationError("connector name must not be blank")
    settings_json = _settings_json(settings)
    if conn.execute(text("SELECT 1 FROM connector WHERE name = :name"), {"name": name}).first() is not None:
        raise ConflictError(f"connector {name!r} already exists")
    connector_id = conn.execute(
        text(
            "INSERT INTO connector (type, name, settings_json, credential_ref)"
            " VALUES (:type, :name, :settings_json, :credential_ref) RETURNING id"
        ),
        {"type": type_name, "name": name, "settings_json": settings_json, "credential_ref": credential_ref},
    ).scalar_one()
    return get_connector(conn, connector_id)


def get_connector(conn: sqlalchemy.Connection, ref: str | int) -> ConnectorRow:
    """Look a connector up by id (int or all-digit string) or by exact name."""
    if isinstance(ref, int) or ref.isdigit():
        where, params = "id = :ref", {"ref": int(ref)}
    else:
        where, params = "name = :ref", {"ref": ref}
    row = conn.execute(text(f"SELECT {_COLUMNS} FROM connector WHERE {where}"), params).mappings().first()
    if row is None:
        raise NotFoundError(f"connector {ref} not found")
    return _row(row)


def list_connectors(conn: sqlalchemy.Connection, *, type_name: str | None = None) -> list[ConnectorRow]:
    where = "" if type_name is None else " WHERE type = :type"
    rows = conn.execute(
        text(f"SELECT {_COLUMNS} FROM connector{where} ORDER BY name"),
        {} if type_name is None else {"type": type_name},
    ).mappings()
    return [_row(row) for row in rows]


def set_enabled(conn: sqlalchemy.Connection, ref: str | int, enabled: bool) -> ConnectorRow:
    """Enable or disable one connector instance (CON-4)."""
    row = get_connector(conn, ref)
    conn.execute(
        text("UPDATE connector SET enabled = :enabled WHERE id = :id"),
        {"enabled": int(enabled), "id": row.id},
    )
    return get_connector(conn, row.id)


def update_settings(conn: sqlalchemy.Connection, ref: str | int, settings: Mapping) -> ConnectorRow:
    """Replace the non-secret settings of a connector instance."""
    row = get_connector(conn, ref)
    conn.execute(
        text("UPDATE connector SET settings_json = :settings_json WHERE id = :id"),
        {"settings_json": _settings_json(settings), "id": row.id},
    )
    return get_connector(conn, row.id)


def set_credential_ref(
    conn: sqlalchemy.Connection, ref: str | int, credential_ref: str | None
) -> ConnectorRow:
    """Point a connector instance at the Credential Manager entry that holds its secret (GOV-8)."""
    row = get_connector(conn, ref)
    conn.execute(
        text("UPDATE connector SET credential_ref = :credential_ref WHERE id = :id"),
        {"credential_ref": credential_ref, "id": row.id},
    )
    return get_connector(conn, row.id)


def instantiate(row: ConnectorRow, **backends: object) -> object:
    """Construct the plugin for a configured instance; backends are injected system access (CON-6)."""
    cls = get_connector_class(row.type)
    return cls(to_config(row), **backends)


def match_reference(
    instances: Sequence[tuple[ConnectorRow, object]], text: str
) -> list[tuple[ConnectorRow, SourceRef]]:
    """Ask every enabled instance in turn whether it recognises `text` as a reference."""
    matches: list[tuple[ConnectorRow, SourceRef]] = []
    for row, instance in instances:
        if not row.enabled:
            continue
        ref = instance.parse_reference(text)  # type: ignore[attr-defined]
        if ref is not None:
            matches.append((row, ref))
    return matches
