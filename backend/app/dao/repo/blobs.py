"""Content-addressed blob store for workbook snapshots and uploaded contract notes."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


class BlobStore:
    def __init__(self, root: Path, suffix: str):
        self.root = root
        self.suffix = suffix
        root.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        path = self.path(sha)
        if not path.exists():
            # atomic write: temp file in same dir + rename
            fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        return sha

    def get(self, sha: str) -> bytes:
        if not all(c in "0123456789abcdef" for c in sha) or len(sha) != 64:
            raise ValueError("bad blob id")
        return self.path(sha).read_bytes()

    def delete(self, sha: str) -> None:
        p = self.path(sha)
        if p.exists():
            p.unlink()

    def path(self, sha: str) -> Path:
        return self.root / f"{sha}{self.suffix}"
