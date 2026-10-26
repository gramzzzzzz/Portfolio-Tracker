"""One place that combines the tracking sheet with the app-owned sheets.

``state()`` returns the *effective* portfolio:
  * ``sheet`` - the original tracking sheet as read (purchase rows untouched);
  * ``snap`` - the same snapshot with its lots replaced by the lots still held after the
    sells / bonus / splits recorded in the ``Transactions`` sheet. Every analytic, the tax
    planner and the price sync use this, so when nothing was sold it is identical to the
    sheet;
  * ``positions`` - sales with FIFO legs (realised gains), issues;
  * ``extras`` - raw Transactions + Gold rows.

Cached per (HEAD version, content hash) so it is computed once per sheet change.
"""
from __future__ import annotations

import dataclasses
import threading
from dataclasses import dataclass

from app.dao.xlsx.extra_sheets import ExtraSheetsDAO
from app.domain.models import Extras, Issue, SheetSnapshot
from app.services import positions as P
from app.services.version_service import VersionService


@dataclass
class PortfolioState:
    sheet: SheetSnapshot
    snap: SheetSnapshot
    positions: P.Positions
    extras: Extras


class PortfolioService:
    def __init__(self, versions: VersionService, extras_dao: ExtraSheetsDAO):
        self.versions = versions
        self.extras_dao = extras_dao
        self._cache: tuple[tuple, PortfolioState] | None = None
        self._lock = threading.Lock()

    def state(self) -> PortfolioState:
        head = self.versions.head()
        sheet = self.versions.snapshot()  # raises NoSheetError when no sheet
        key = (head["id"], head["sha256"])
        with self._lock:
            if self._cache and self._cache[0] == key:
                return self._cache[1]
        extras = self.extras_dao.read(self.versions.head_bytes())
        pos = P.build(sheet.lots, extras.transactions)
        issues = list(sheet.issues)
        issues += [Issue(i["severity"], i["code"], i["message"]) for i in pos.issues]
        issues += [Issue("warning", "app_sheet", w) for w in extras.warnings]
        snap = dataclasses.replace(sheet, lots=pos.open_lots, issues=issues)
        st = PortfolioState(sheet, snap, pos, extras)
        with self._lock:
            self._cache = (key, st)
        return st

    def snapshot(self) -> SheetSnapshot:
        return self.state().snap
