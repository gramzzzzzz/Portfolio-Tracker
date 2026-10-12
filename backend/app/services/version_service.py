"""Version management for the tracking sheet (the application's model).

Every structural change (initial upload, contract-note import, manual upload, restore) creates
an immutable, content-addressed snapshot. The newest version is the HEAD (the current sheet).
Restoring an old snapshot never deletes history: it creates a new version whose content
equals the old one (like `git revert`, not `git reset`).

Exception, by the owner's choice: the periodic market-price refresh rewrites the HEAD
version *in place* (only market-rate cells and the valuation date change), so hourly price
updates do not flood the history. The superseded blob is deleted when no version uses it.

Every write happens under :class:`SheetLock` and the current HEAD is mirrored to
``<data>/current/<filename>`` (atomic replace) for terminal / file-share access.
"""
from __future__ import annotations

import os
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Callable

from app.dao.base import LayoutError, PortfolioSheetDAO
from app.dao.repo.blobs import BlobStore
from app.dao.repo.sqlite_repo import Repository
from app.domain.models import SheetSnapshot
from app.services.sheet_lock import SheetLock


class NoSheetError(Exception):
    pass


class VersionService:
    MIRROR_NAME = "Invt.xlsx"

    def __init__(self, repo: Repository, blobs: BlobStore, sheet_dao: PortfolioSheetDAO,
                 lock: SheetLock, mirror_dir: Path | None = None):
        self.repo = repo
        self.blobs = blobs
        self.dao = sheet_dao
        self.write_lock = lock  # serialises every mutation of HEAD (threads + processes)
        self.mirror_dir = mirror_dir
        self.on_change: Callable[[], None] | None = None  # e.g. kick a price sync after imports
        self._snap_cache: tuple[tuple[int, str], SheetSnapshot] | None = None
        if mirror_dir is not None and self.head_id() is not None and not self.mirror_path.exists():
            self._mirror(self.head_bytes())

    # ---------------------------------------------------------------- reads
    def head_id(self) -> int | None:
        return self.repo.get("head_version_id")

    def head(self) -> dict | None:
        hid = self.head_id()
        return self.repo.version(hid) if hid else None

    def head_bytes(self) -> bytes:
        h = self.head()
        if not h:
            raise NoSheetError("No tracking sheet uploaded yet")
        return self.blobs.get(h["sha256"])

    def bytes_of(self, vid: int) -> bytes:
        v = self.repo.version(vid)
        if not v:
            raise KeyError(vid)
        return self.blobs.get(v["sha256"])

    def snapshot(self) -> SheetSnapshot:
        hid = self.head_id()
        if hid is None:
            raise NoSheetError("No tracking sheet uploaded yet")
        h = self.repo.version(hid)
        key = (hid, h["sha256"])  # content can change in place (price refresh)
        if self._snap_cache and self._snap_cache[0] == key:
            return self._snap_cache[1]
        snap = self.dao.read(self.blobs.get(h["sha256"]))
        self._snap_cache = (key, snap)
        return snap

    @property
    def mirror_path(self) -> Path:
        return (self.mirror_dir or Path(".")) / self.MIRROR_NAME

    def _mirror(self, data: bytes) -> None:
        """Atomically replace <data>/current/Invt.xlsx with the HEAD content."""
        if self.mirror_dir is None:
            return
        self.mirror_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.mirror_dir, prefix=".tmp-", suffix=".xlsx")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.mirror_path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def list(self) -> list[dict]:
        hid = self.head_id()
        out = self.repo.versions()
        for v in out:
            v["is_head"] = v["id"] == hid
        return out

    # --------------------------------------------------------------- writes
    def commit(self, data: bytes, *, message: str, source: str, batch_id: str | None = None,
               filename: str | None = None, expected_parent: int | None | str = "any") -> dict:
        """Store ``data`` as the new HEAD. Validates that the DAO can read it."""
        snap = self.dao.read(data)  # raises LayoutError if unusable
        with self.write_lock:
            parent = self.head_id()
            if expected_parent != "any" and expected_parent != parent:
                raise ConflictError("The sheet changed while you were working; please review again")
            sha = self.blobs.put(data)
            stats = {
                "lots": len(snap.lots),
                "holdings": len({l.name for l in snap.lots}),
                "invested": snap.totals.get("grand_total_cost"),
                "issues": len([i for i in snap.issues if i.severity != "info"]),
            }
            if parent:
                prev = self.repo.version(parent)
                stats["lots_delta"] = stats["lots"] - (prev["stats"].get("lots") or 0)
            vid = self.repo.add_version(sha256=sha, size=len(data), message=message, source=source,
                                        parent_id=parent, batch_id=batch_id, filename=filename, stats=stats)
            self.repo.put("head_version_id", vid)
            self.repo.audit("version.commit", {"version": vid, "source": source, "message": message})
            self._snap_cache = ((vid, sha), snap)
            self._mirror(data)
        if self.on_change:
            self.on_change()
        return self.repo.version(vid)

    def replace_head_content(self, data: bytes, *, note: str) -> dict:
        """Swap HEAD's content without creating a version (market-price refresh only).
        Caller must hold ``write_lock`` and must have produced ``data`` from the current HEAD."""
        with self.write_lock:
            head = self.head()
            if not head:
                raise NoSheetError("No tracking sheet uploaded yet")
            snap = self.dao.read(data)
            old_sha = head["sha256"]
            sha = self.blobs.put(data)
            self.repo.update_version_content(head["id"], sha, len(data), time.time())
            if sha != old_sha and not self.repo.sha_in_use(old_sha):
                self.blobs.delete(old_sha)
            self._snap_cache = ((head["id"], sha), snap)
            self._mirror(data)
            self.repo.audit("prices.write", {"version": head["id"], "message": note})
            return self.repo.version(head["id"])

    def upload(self, data: bytes, filename: str, message: str | None = None) -> dict:
        try:
            self.dao.read(data)
        except LayoutError as e:
            raise LayoutError(f"Uploaded file is not a compatible tracking sheet: {e}") from e
        src = "initial" if self.head_id() is None else "upload"
        return self.commit(data, message=message or f"Uploaded {filename}", source=src, filename=filename)

    def restore(self, vid: int) -> dict:
        v = self.repo.version(vid)
        if not v:
            raise KeyError(vid)
        return self.commit(self.blobs.get(v["sha256"]), message=f"Restored version #{vid}", source="restore")

    # ---------------------------------------------------------------- diff
    def diff(self, vid: int, against: int | None = None) -> dict:
        v = self.repo.version(vid)
        if not v:
            raise KeyError(vid)
        base_id = against if against is not None else v["parent_id"]
        new = self.dao.read(self.blobs.get(v["sha256"]))
        old = self.dao.read(self.bytes_of(base_id)) if base_id else None

        def sig(l):
            return (l.name, str(l.date), round(l.qty, 6), round(l.rate, 4), round(l.charges, 2))

        new_c = Counter(sig(l) for l in new.lots)
        old_c = Counter(sig(l) for l in old.lots) if old else Counter()
        added = [l for l in new.lots if new_c[sig(l)] - old_c.get(sig(l), 0) > 0]
        removed = [l for l in old.lots if old_c[sig(l)] - new_c.get(sig(l), 0) > 0] if old else []
        old_sum = {s.name for s in old.summary if s.name} if old else set()
        old_sec = {s.name for s in old.sectors} if old else set()
        return {
            "version": vid,
            "against": base_id,
            "added_lots": [_lot_dict(l) for l in added],
            "removed_lots": [_lot_dict(l) for l in removed],
            "added_summary": [s.name for s in new.summary if s.name and s.name not in old_sum],
            "added_sectors": [s.name for s in new.sectors if s.name not in old_sec],
            "invested_before": old.totals.get("grand_total_cost") if old else None,
            "invested_after": new.totals.get("grand_total_cost"),
        }


class ConflictError(Exception):
    pass


def _lot_dict(l) -> dict:
    return {"row": l.row, "name": l.name, "date": str(l.date) if l.date else None, "qty": l.qty,
            "rate": l.rate, "charges": l.charges, "cost": l.cost, "block": l.block}
