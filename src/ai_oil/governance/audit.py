"""Append-only audit log (GOV-7). Command handlers log class B actions; sync and AI log their own."""

import json
from collections.abc import Mapping
from dataclasses import dataclass

import sqlalchemy
from sqlalchemy import text

from ai_oil.errors import ValidationError
from ai_oil.governance.classes import ACTION_CLASSES
from ai_oil.timeutil import utcnow_iso

ACTOR_TYPES = ("user", "sync", "ai", "system")
_FORBIDDEN_KEY_PARTS = ("token", "password", "secret")


@dataclass(frozen=True)
class AuditEntry:
    """One audited action as recorded (GOV-7)."""

    id: int
    at: str
    actor_type: str
    action: str
    action_class: str
    target: str | None
    detail: dict
    approval_id: int | None


def log(
    conn: sqlalchemy.Connection,
    action: str,
    action_class: str,
    *,
    actor_type: str,
    target: str | None = None,
    detail: Mapping[str, object] | None = None,
    approval_id: int | None = None,
) -> int:
    """Append one audit entry and return its id (GOV-7, GOV-8)."""
    if actor_type not in ACTOR_TYPES:
        raise ValidationError(f"actor_type must be one of {', '.join(ACTOR_TYPES)}, got {actor_type!r}")
    if action_class not in ACTION_CLASSES:
        raise ValidationError(
            f"action_class must be one of {', '.join(ACTION_CLASSES)}, got {action_class!r}"
        )
    if not action.strip():
        raise ValidationError("audit action must not be blank")
    for key in detail or {}:
        if any(part in key.lower() for part in _FORBIDDEN_KEY_PARTS):
            raise ValidationError(f"audit detail must not contain secrets (GOV-8): {key}")
    detail_json = json.dumps(dict(detail or {}), sort_keys=True, ensure_ascii=False, default=str)
    result = conn.execute(
        text(
            "INSERT INTO audit_log (at, actor_type, action, action_class, target, detail_json, approval_id)"
            " VALUES (:at, :actor_type, :action, :action_class, :target, :detail_json, :approval_id)"
            " RETURNING id"
        ),
        {
            "at": utcnow_iso(),
            "actor_type": actor_type,
            "action": action,
            "action_class": action_class,
            "target": target,
            "detail_json": detail_json,
            "approval_id": approval_id,
        },
    )
    return result.scalar_one()


def list_entries(
    conn: sqlalchemy.Connection,
    *,
    limit: int = 100,
    action_prefix: str | None = None,
) -> list[AuditEntry]:
    """Newest audit entries first, optionally only actions starting with a prefix (GOV-7)."""
    sql = "SELECT id, at, actor_type, action, action_class, target, detail_json, approval_id FROM audit_log"
    params: dict[str, object] = {"limit": limit}
    if action_prefix is not None:
        sql += " WHERE action LIKE :prefix || '%'"
        params["prefix"] = action_prefix
    sql += " ORDER BY id DESC LIMIT :limit"
    entries = []
    for row in conn.execute(text(sql), params).mappings():
        data = dict(row)
        data["detail"] = json.loads(data.pop("detail_json"))
        entries.append(AuditEntry(**data))
    return entries
