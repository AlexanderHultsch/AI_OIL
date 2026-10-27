from pathlib import Path

import pytest

from ai_oil.cli.main import main
from ai_oil.config import load_config
from ai_oil.db.engine import create_db_engine
from ai_oil.domain import people

ARGS = [
    "init",
    "--name",
    "Alex",
    "--email",
    "Alex@Example.com",
    "--email",
    "a2@example.com",
    "--atlassian-account-id",
    "acc-1",
]


def test_init_creates_home_database_and_me(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "h"

    assert main(["--home", str(home), *ARGS]) == 0

    assert capsys.readouterr().out.splitlines() == [
        f"Initialised AI_OIL at {home}",
        f"Database: {home / 'ai_oil.db'}",
        "Me: Alex <alex@example.com, a2@example.com>",
    ]
    assert (home / "config.toml").is_file()
    assert (home / "blobs").is_dir()
    assert (home / "logs").is_dir()
    assert (home / "ai_oil.db").is_file()

    engine = create_db_engine(load_config(home).db_path)
    try:
        with engine.connect() as conn:
            me = people.get_me(conn)
            identities = people.list_identities(conn, me.id)
    finally:
        engine.dispose()
    assert me.display_name == "Alex"
    assert [(i.system, i.external_id) for i in identities] == [
        ("atlassian", "acc-1"),
        ("email", "a2@example.com"),
        ("email", "alex@example.com"),
    ]


def test_second_init_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "h"
    assert main(["--home", str(home), *ARGS]) == 0
    capsys.readouterr()

    assert main(["--home", str(home), *ARGS]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: already initialised at {home}\n"


def test_init_requires_an_email(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--home", str(tmp_path / "h"), "init", "--name", "Alex"])
    assert excinfo.value.code == 2
