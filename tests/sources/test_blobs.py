import gzip
import hashlib
from pathlib import Path

import pytest
import sqlalchemy

from ai_oil.errors import NotFoundError
from ai_oil.sources.blobs import BlobStore


@pytest.fixture
def store(tmp_path: Path) -> BlobStore:
    return BlobStore(tmp_path / "blobs", max_bytes=100)


def _files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def test_put_stores_gzip_under_hash_prefix_and_indexes_it(
    conn: sqlalchemy.Connection, store: BlobStore
) -> None:
    data = b"hello raw original"
    blob_hash = store.put(conn, data, "text/plain")
    assert blob_hash == hashlib.sha256(data).hexdigest()
    path = store.root / blob_hash[:2] / f"{blob_hash}.gz"
    assert _files(store.root) == [path]
    assert gzip.decompress(path.read_bytes()) == data
    row = conn.execute(sqlalchemy.text("SELECT * FROM blob")).mappings().one()
    assert row["hash"] == blob_hash
    assert row["size_bytes"] == len(data)
    assert row["media_type"] == "text/plain"
    assert row["stored_path"] == f"{blob_hash[:2]}/{blob_hash}.gz"
    assert row["created_at"]


def test_same_bytes_are_stored_once(conn: sqlalchemy.Connection, store: BlobStore) -> None:
    first = store.put(conn, b"same")
    second = store.put(conn, b"same", "text/plain")
    assert first == second
    assert conn.execute(sqlalchemy.text("SELECT count(*) FROM blob")).scalar_one() == 1
    assert len(_files(store.root)) == 1


def test_existing_hash_is_not_rewritten(conn: sqlalchemy.Connection, store: BlobStore) -> None:
    blob_hash = store.put(conn, b"same")
    path = store.root / blob_hash[:2] / f"{blob_hash}.gz"
    path.write_bytes(b"sentinel")
    store.put(conn, b"same")
    assert path.read_bytes() == b"sentinel"


def test_get_round_trip(conn: sqlalchemy.Connection, store: BlobStore) -> None:
    data = bytes(range(50))
    assert store.get(conn, store.put(conn, data)) == data
    assert store.get(conn, store.put(conn, b"")) == b""


def test_get_unknown_hash_raises(conn: sqlalchemy.Connection, store: BlobStore) -> None:
    with pytest.raises(NotFoundError):
        store.get(conn, "0" * 64)


def test_too_large_is_not_stored(conn: sqlalchemy.Connection, store: BlobStore) -> None:
    assert store.put(conn, b"x" * 101) is None
    assert conn.execute(sqlalchemy.text("SELECT count(*) FROM blob")).scalar_one() == 0
    assert not store.root.exists() or _files(store.root) == []
    assert store.put(conn, b"x" * 100) is not None


def test_no_temp_files_are_left_behind(conn: sqlalchemy.Connection, store: BlobStore) -> None:
    store.put(conn, b"one")
    store.put(conn, b"two")
    assert all(path.suffix == ".gz" for path in _files(store.root))
    assert len(_files(store.root)) == 2


def test_failed_write_removes_the_temp_file(
    conn: sqlalchemy.Connection, store: BlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("ai_oil.sources.blobs.os.replace", fail)
    with pytest.raises(OSError, match="disk full"):
        store.put(conn, b"data")
    assert _files(store.root) == []
    assert conn.execute(sqlalchemy.text("SELECT count(*) FROM blob")).scalar_one() == 0
