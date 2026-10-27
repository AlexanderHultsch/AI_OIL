"""Content-addressed store for raw originals: <root>/<aa>/<sha256>.gz (spec 5.11.5)."""

import gzip
import hashlib
import os
import tempfile
from pathlib import Path

import sqlalchemy
from sqlalchemy import text

from ai_oil.errors import NotFoundError
from ai_oil.timeutil import utcnow_iso


class BlobStore:
    """Raw originals on disk, keyed by the sha256 of their bytes; `blob` rows index them."""

    def __init__(self, root: Path, max_bytes: int) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes

    def put(self, conn: sqlalchemy.Connection, data: bytes, media_type: str | None = None) -> str | None:
        """Store `data` once and return its hash; None when it exceeds `max_bytes` (nothing is stored)."""
        if len(data) > self.max_bytes:
            return None
        blob_hash = hashlib.sha256(data).hexdigest()
        exists = conn.execute(text("SELECT 1 FROM blob WHERE hash = :hash"), {"hash": blob_hash}).first()
        if exists is not None:
            return blob_hash
        relative = f"{blob_hash[:2]}/{blob_hash}.gz"
        self._write_atomic(self.root / relative, gzip.compress(data, mtime=0))
        conn.execute(
            text(
                "INSERT INTO blob (hash, size_bytes, media_type, stored_path, created_at)"
                " VALUES (:hash, :size, :media_type, :stored_path, :now)"
            ),
            {
                "hash": blob_hash,
                "size": len(data),
                "media_type": media_type,
                "stored_path": relative,
                "now": utcnow_iso(),
            },
        )
        return blob_hash

    def get(self, conn: sqlalchemy.Connection, blob_hash: str) -> bytes:
        stored_path = conn.execute(
            text("SELECT stored_path FROM blob WHERE hash = :hash"), {"hash": blob_hash}
        ).scalar_one_or_none()
        if stored_path is None:
            raise NotFoundError(f"blob {blob_hash} not found")
        return gzip.decompress((self.root / stored_path).read_bytes())

    @staticmethod
    def _write_atomic(target: Path, payload: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
