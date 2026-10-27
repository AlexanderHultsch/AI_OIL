from pathlib import Path

import pytest
import sqlalchemy

from ai_oil.config import load_config
from ai_oil.db.engine import create_db_engine
from ai_oil.sources import linking, registry, store

LIBRARY_URL = "https://acme.sharepoint.com/sites/HW/Shared%20Documents"
FILE_URL = f"{LIBRARY_URL}/Specs/Torque%20spec.txt"


@pytest.fixture
def synced_file(tmp_path: Path) -> Path:
    library = tmp_path / "OneDrive" / "HW - Shared Documents"
    (library / "Specs").mkdir(parents=True)
    path = library / "Specs" / "Torque spec.txt"
    path.write_text("The flange torque limit is 12 Nm.", encoding="utf-8")
    return path


@pytest.fixture
def sharepoint_connector(home, synced_file: Path) -> None:
    engine = create_db_engine(load_config(home).db_path)
    try:
        with engine.begin() as conn:
            registry.create_connector(
                conn,
                "sharepoint",
                "SharePoint",
                settings={
                    "sync_roots": [
                        {
                            "url": "https://acme.sharepoint.com/sites/HW/Shared Documents",
                            "path": str(synced_file.parents[1]),
                        }
                    ]
                },
            )
    finally:
        engine.dispose()


@pytest.fixture
def issue(oil, capsys: pytest.CaptureFixture[str]) -> str:
    assert oil("project", "add", "OIL", "Open issues") == 0
    assert oil("issue", "create", "-p", "OIL", "-s", "Fit the filter", "-d", "Needs a part") == 0
    capsys.readouterr()
    return "OIL-1"


def run(oil, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = oil(*argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def file_objects(conn: sqlalchemy.Connection) -> list[sqlalchemy.Row]:
    return list(
        conn.execute(
            sqlalchemy.text(
                "SELECT id, object_type, external_id, display_ref, locator, fetch_status FROM source_object"
                " WHERE object_type IN ('file', 'web_link')"
            )
        ).all()
    )


def test_link_a_sharepoint_url_maps_to_the_synced_file_and_finds_it_by_text(
    oil, db, capsys, issue, synced_file, sharepoint_connector
) -> None:
    code, out, err = run(oil, capsys, "link", issue, FILE_URL)

    assert (code, err) == (0, "")
    assert out.splitlines()[0] == "Linked file: Torque spec.txt to OIL-1"
    with db() as conn:
        [obj] = file_objects(conn)
        versions = store.list_versions(conn, obj.id)
        assert linking.linked_object_ids(conn, issue) == [obj.id]
    assert (obj.object_type, obj.external_id, obj.display_ref, obj.locator, obj.fetch_status) == (
        "file",
        str(synced_file),
        "Torque spec.txt",
        FILE_URL,
        "ok",
    )
    assert f"Sync source:{obj.id}: ok" in out
    assert "1 new versions, 0 new entries" in out
    assert [v.text for v in versions] == ["The flange torque limit is 12 Nm."]

    code, found, _ = run(oil, capsys, "search", 'source = sharepoint AND text ~ "flange"')
    assert code == 0
    assert "OIL-1" in found


def test_link_a_local_path_to_a_synced_file(
    oil, db, capsys, issue, synced_file, sharepoint_connector
) -> None:
    code, out, _ = run(oil, capsys, "link", issue, str(synced_file))

    assert code == 0
    assert out.splitlines()[0] == "Linked file: Torque spec.txt to OIL-1"
    with db() as conn:
        [obj] = file_objects(conn)
    assert (obj.object_type, obj.external_id, obj.locator) == ("file", str(synced_file), str(synced_file))


def test_a_url_of_an_unsynced_library_is_rejected(oil, db, capsys, issue, sharepoint_connector) -> None:
    code, out, err = run(
        oil, capsys, "link", issue, "https://acme.sharepoint.com/sites/Quality/Shared%20Documents/a.docx"
    )

    assert code == 1
    assert out == ""
    assert "this SharePoint library is not synced on this computer" in err
    assert "acme.sharepoint.com/sites/Quality/Shared Documents" in err
    with db() as conn:
        assert file_objects(conn) == []
