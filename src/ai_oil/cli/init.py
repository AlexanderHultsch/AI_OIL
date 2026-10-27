"""`oil init`: create the data folder, the database and the 'me' person."""

import argparse

from ai_oil.config import default_home, load_config, write_default_config
from ai_oil.db.engine import create_db_engine
from ai_oil.db.migrate import upgrade
from ai_oil.domain import people
from ai_oil.errors import ConfigError, ConflictError, NotFoundError
from ai_oil.setup.probe import LocalProbe, inside_onedrive


def register(subparsers) -> None:
    parser = subparsers.add_parser("init", help="create the data folder and the 'me' person")
    parser.add_argument("--name", required=True, help="your display name")
    parser.add_argument(
        "--email", action="append", required=True, help="your e-mail address (repeat for several)"
    )
    parser.add_argument("--atlassian-account-id", default=None, help="your Jira/Confluence accountId")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    home = args.home or default_home()
    root = inside_onedrive(home, LocalProbe().onedrive_roots())
    if root is not None:
        raise ConfigError(
            f"{home} is inside OneDrive ({root}) — a database under a sync client can be corrupted; "
            "use the default %LOCALAPPDATA%\\AI_OIL or set AI_OIL_HOME elsewhere"
        )
    write_default_config(home)
    config = load_config(home)
    engine = create_db_engine(config.db_path)
    try:
        upgrade(engine)
        with engine.begin() as conn:
            try:
                people.get_me(conn)
            except NotFoundError:
                pass
            else:
                raise ConflictError(f"already initialised at {home}")
            me = people.create_person(conn, args.name, is_me=True)
            emails: list[str] = []
            for address in args.email:
                identity = people.add_identity(conn, me.id, "email", address)
                if identity.external_id not in emails:
                    emails.append(identity.external_id)
            if args.atlassian_account_id:
                people.add_identity(conn, me.id, "atlassian", args.atlassian_account_id)
    finally:
        engine.dispose()
    print(f"Initialised AI_OIL at {home}")
    print(f"Database: {config.db_path}")
    print(f"Me: {me.display_name} <{', '.join(emails)}>")
    return 0
