"""Hand-entered events in the ``Transactions`` sheet: sells, bonus shares, splits.

Every change rewrites only the Transactions sheet (the original tracking sheet is verified
byte-for-byte unchanged by the writer) and is saved as a new sheet version, so it can be
undone from Sheet history like an import.
"""
from __future__ import annotations

import datetime as dt

from app.dao.xlsx.extra_sheets import ExtraSheetsDAO, new_id
from app.domain.models import Transaction, TxnType
from app.services import positions as P
from app.services.portfolio_service import PortfolioService
from app.services.version_service import VersionService


class TransactionsService:
    def __init__(self, versions: VersionService, extras: ExtraSheetsDAO, portfolio: PortfolioService):
        self.versions = versions
        self.extras = extras
        self.portfolio = portfolio

    def list(self) -> dict:
        st = self.portfolio.state()
        sales = {s.txn.id: s for s in st.positions.sales}
        out = []
        for t in sorted(st.extras.transactions, key=lambda x: (x.date, x.id), reverse=True):
            d = {
                "id": t.id, "date": t.date.isoformat(), "type": t.type.value, "name": t.name, "qty": t.qty,
                "price": t.price, "value": t.proceeds if t.type == TxnType.SELL else None, "charges": t.charges,
                "stt": t.stt, "ratio": t.ratio, "isin": t.isin, "asset_class": t.asset_class, "broker": t.broker,
                "note_no": t.note_no, "source": t.source, "remarks": t.remarks, "row": t.row,
            }
            s = sales.get(t.id)
            if s:
                d["legs"] = [{"row": l.lot_row if l.origin == "purchase" else None, "origin": l.origin,
                              "purchase_date": l.purchase_date.isoformat(), "units": l.units} for l in s.legs]
                d["unmatched_units"] = s.unmatched_units
            out.append(d)
        return {"transactions": out, "issues": [i for i in st.positions.issues],
                "securities": sorted({l.name for l in st.sheet.lots}), "warnings": st.extras.warnings}

    def _validate(self, t: Transaction, others: list[Transaction]) -> None:
        st = self.portfolio.state()
        names = {l.name.casefold(): l.name for l in st.sheet.lots}
        if t.name.casefold() not in names:
            raise ValueError(f"'{t.name}' has no purchase rows in the sheet")
        t.name = names[t.name.casefold()]
        if t.date > dt.date.today():
            raise ValueError("The date cannot be in the future")
        if t.type == TxnType.SELL:
            if t.qty <= 0 or not t.price or t.price <= 0:
                raise ValueError("A sale needs units and a price")
            if t.charges < 0 or t.stt < 0:
                raise ValueError("Charges cannot be negative")
            # units held on the sale date, applying all OTHER recorded events
            pos = P.build(st.sheet.lots, [o for o in others if o.id != t.id], as_of=t.date)
            held = pos.held(t.name)
            if t.qty > held + 1e-6:
                raise ValueError(f"On {t.date:%d %b %Y} you held {held:g} units of {t.name}; cannot sell {t.qty:g}")
        elif t.type == TxnType.BONUS:
            if t.qty <= 0:
                raise ValueError("Enter the number of bonus shares received")
        elif t.type == TxnType.SPLIT:
            if not t.ratio or t.ratio <= 0:
                raise ValueError("Enter the split ratio as new shares per old share (e.g. 5 for 1:5, 2 for 1:2)")
            t.qty = 0.0

    def _commit(self, txns: list[Transaction], message: str) -> dict:
        with self.versions.write_lock:
            data = self.versions.head_bytes()
            new = self.extras.write(data, transactions=txns)
            v = self.versions.commit(new, message=message, source="transactions")
        return {"version": v, **self.list()}

    def add(self, t: Transaction) -> dict:
        current = self.portfolio.state().extras.transactions
        t.id = t.id or new_id("T")
        t.source = t.source or "manual"
        self._validate(t, current)
        return self._commit(current + [t], f"Recorded {t.type.value.lower()} of {t.name} on {t.date:%d %b %Y}")

    def update(self, txn_id: str, t: Transaction) -> dict:
        current = self.portfolio.state().extras.transactions
        if not any(x.id == txn_id for x in current):
            raise KeyError(txn_id)
        t.id = txn_id
        self._validate(t, current)
        return self._commit([t if x.id == txn_id else x for x in current], f"Edited {t.type.value.lower()} {txn_id}")

    def delete(self, txn_id: str) -> dict:
        current = self.portfolio.state().extras.transactions
        keep = [x for x in current if x.id != txn_id]
        if len(keep) == len(current):
            raise KeyError(txn_id)
        gone = next(x for x in current if x.id == txn_id)
        # deleting a bonus/split can make later sells exceed the holding: check
        pos = P.build(self.portfolio.state().sheet.lots, keep)
        bad = [i for i in pos.issues if i["code"] == "sell_exceeds_holding"]
        if bad:
            raise ValueError("Cannot delete: " + bad[0]["message"])
        return self._commit(keep, f"Deleted {gone.type.value.lower()} of {gone.name} on {gone.date:%d %b %Y}")
