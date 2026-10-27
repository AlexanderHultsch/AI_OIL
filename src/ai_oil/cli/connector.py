"""`oil connector`: configure, check, enable and disable connector instances (CON-4, CON-6, GOV-8)."""

import argparse
import getpass
import json
import sys

from ai_oil import secrets
from ai_oil.cli.common import transaction
from ai_oil.errors import ValidationError
from ai_oil.governance import audit
from ai_oil.sources import health, registry


def _add_setting_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--setting",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        help="a non-secret setting; VALUE is read as JSON when it parses, else as text (repeat for several)",
    )


def _add_token_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--token", action="store_true", help="prompt for the API token (stored in the Credential Manager)"
    )
    parser.add_argument(
        "--token-stdin", action="store_true", help="read the API token from one line of stdin"
    )


def register(subparsers) -> None:
    connector = subparsers.add_parser("connector", help="manage connector instances")
    sub = connector.add_subparsers(dest="connector_command", required=True)

    add = sub.add_parser("add", help="configure a connector instance")
    add.add_argument(
        "type", metavar="TYPE", help="connector type, e.g. outlook, jira, confluence, sharepoint"
    )
    add.add_argument("name", metavar="NAME", help="name of the instance")
    _add_setting_options(add)
    _add_token_options(add)
    add.set_defaults(func=run_add)

    set_ = sub.add_parser("set", help="change the settings or the token of a connector instance")
    set_.add_argument("name", metavar="NAME", help="connector name or id")
    _add_setting_options(set_)
    set_.add_argument("--unset", action="append", default=None, metavar="KEY", help="remove a setting")
    _add_token_options(set_)
    set_.set_defaults(func=run_set)

    list_ = sub.add_parser("list", help="list connector instances")
    list_.set_defaults(func=run_list)

    check = sub.add_parser("check", help="check the configuration of connector instances")
    check.add_argument(
        "name", metavar="NAME", nargs="?", default=None, help="connector name or id (default: all)"
    )
    check.add_argument(
        "--live", action="store_true", help="also run the read-only check against the real system"
    )
    check.set_defaults(func=run_check)

    enable = sub.add_parser("enable", help="enable a connector instance")
    enable.add_argument("name", metavar="NAME", help="connector name or id")
    enable.set_defaults(func=run_enable)

    disable = sub.add_parser("disable", help="disable a connector instance")
    disable.add_argument("name", metavar="NAME", help="connector name or id")
    disable.set_defaults(func=run_disable)


def _parse_settings(items: list[str] | None) -> dict[str, object]:
    settings: dict[str, object] = {}
    for item in items or []:
        key, separator, raw = item.partition("=")
        key = key.strip()
        if not separator or not key:
            raise ValidationError(f"invalid --setting {item!r}: use KEY=VALUE")
        try:
            settings[key] = json.loads(raw)
        except ValueError:
            settings[key] = raw
    return settings


def _read_token(args: argparse.Namespace) -> str | None:
    if args.token and args.token_stdin:
        raise ValidationError("choose --token or --token-stdin")
    if args.token:
        token = getpass.getpass("API token: ")
    elif args.token_stdin:
        token = sys.stdin.readline().strip()
    else:
        return None
    if not token.strip():
        raise ValidationError("the API token must not be empty")
    return token


def run_add(args: argparse.Namespace) -> int:
    settings = _parse_settings(args.setting)
    token = _read_token(args)
    name = args.name.strip()
    credential_ref = f"ai_oil/{name}" if token is not None else None
    with transaction(args) as conn:
        row = registry.create_connector(
            conn, args.type, name, settings=settings, credential_ref=credential_ref
        )
        registry.instantiate(row)
        audit.log(
            conn,
            "connector.create",
            "B",
            actor_type="user",
            target=row.name,
            detail={
                "type": row.type,
                "settings": row.settings,
                "credential": "stored" if token is not None else "none",
            },
        )
        if token is not None and credential_ref is not None:
            secrets.set_secret(credential_ref, token)
    print(f"Added connector {row.name} ({row.type})")
    return 0


def run_set(args: argparse.Namespace) -> int:
    updates = _parse_settings(args.setting)
    removals = [key.strip() for key in args.unset or []]
    token = _read_token(args)
    if not updates and not removals and token is None:
        raise ValidationError("nothing to change")
    conflicting = sorted(set(updates) & set(removals))
    if conflicting:
        raise ValidationError(f"setting {conflicting[0]} is both set and unset")
    with transaction(args) as conn:
        row = registry.get_connector(conn, args.name)
        merged = {key: value for key, value in {**row.settings, **updates}.items() if key not in removals}
        updated = registry.update_settings(conn, row.id, merged)
        credential_ref = row.credential_ref
        if token is not None:
            if credential_ref is None:
                credential_ref = f"ai_oil/{row.name}"
                updated = registry.set_credential_ref(conn, row.id, credential_ref)
        registry.instantiate(updated)
        audit.log(
            conn,
            "connector.update",
            "B",
            actor_type="user",
            target=row.name,
            detail={
                "type": row.type,
                "settings": updated.settings,
                "credential": "stored" if token is not None else "unchanged",
            },
        )
        if token is not None and credential_ref is not None:
            secrets.set_secret(credential_ref, token)
    print(f"Updated connector {row.name}")
    return 0


def run_list(args: argparse.Namespace) -> int:
    with transaction(args) as conn:
        rows = registry.list_connectors(conn)
    registry.available_connector_types()
    for name, error in registry.load_errors.items():
        print(f"warning: connector plugin {name} failed to load: {error}", file=sys.stderr)
    for row in rows:
        state = "enabled" if row.enabled else "disabled"
        print(f"{row.id}  {row.name}  {row.type}  {state}  credential: {health.credential_state(row)}")
        print(f"    settings: {json.dumps(row.settings, sort_keys=True)}")
    return 0


def run_check(args: argparse.Namespace) -> int:
    with transaction(args) as conn:
        rows = (
            [registry.get_connector(conn, args.name)]
            if args.name is not None
            else registry.list_connectors(conn)
        )
    failed = False
    for row in rows:
        print(f"{row.name} ({row.type})")
        if not row.enabled:
            print("  [--] disabled")
            continue
        connector, lines = health.check_configuration(row)
        if args.live and connector is not None:
            lines += health.check_live(connector)
        failed = failed or any(not line.ok for line in lines)
        for line in lines:
            print(line.format())
    return 1 if failed else 0


def _set_enabled(args: argparse.Namespace, enabled: bool) -> int:
    with transaction(args) as conn:
        row = registry.set_enabled(conn, args.name, enabled)
        audit.log(
            conn,
            "connector.enable" if enabled else "connector.disable",
            "B",
            actor_type="user",
            target=row.name,
            detail={"type": row.type},
        )
    print(f"{'Enabled' if enabled else 'Disabled'} connector {row.name}")
    return 0


def run_enable(args: argparse.Namespace) -> int:
    return _set_enabled(args, True)


def run_disable(args: argparse.Namespace) -> int:
    return _set_enabled(args, False)
