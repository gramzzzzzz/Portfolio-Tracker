"""DAO for the app-owned sheets in the tracking workbook: ``Transactions`` and ``Gold``.

Separate from :mod:`invt_sheet` on purpose: these sheets are fully owned by the app and
rewritten as a whole, while the original tracking sheet is never touched by this module
(enforced by :func:`app_sheets.write_tables`, which verifies it byte-for-byte).
"""
from __future__ import annotations

import datetime as dt
import uuid

from app.domain.models import Extras, GoldItem, Transaction, TxnType

from . import app_sheets as A
from .app_sheets import Column, TableSpec
from .workbook import Workbook

TXN_SPEC = TableSpec(
    "Transactions",
    (
        Column("id", "ID", A.TEXT, 11),
        Column("date", "Date", A.DATE, 13, required=True),
        Column("type", "Type", A.TEXT, 9, required=True),
        Column("name", "Security", A.TEXT, 30, required=True),
        Column("qty", "Qty", A.QTY, 10),
        Column("price", "Price", A.NUM, 12),
        Column("value", "Value", A.NUM, 14),
        Column("charges", "Charges", A.NUM, 11),
        Column("stt", "STT", A.NUM, 9),
        Column("ratio", "Split ratio", A.QTY, 10),
        Column("isin", "ISIN", A.TEXT, 15),
        Column("asset_class", "Asset class", A.TEXT, 12),
        Column("broker", "Broker", A.TEXT, 16),
        Column("note_no", "Contract note", A.TEXT, 15),
        Column("source", "Source", A.TEXT, 14),
        Column("remarks", "Remarks", A.TEXT, 30),
    ),
    "Sells, bonus shares and splits. Purchases stay in the main sheet. Managed by Portfolio Tracker - "
    "you may edit rows here and upload the workbook.",
)

GOLD_SPEC = TableSpec(
    "Gold",
    (
        Column("id", "ID", A.TEXT, 11),
        Column("date", "Purchase date", A.DATE, 13, required=True),
        Column("description", "Description", A.TEXT, 28, required=True),
        Column("form", "Form", A.TEXT, 11),
        Column("purity", "Purity", A.TEXT, 8),
        Column("weight_g", "Weight (g)", A.QTY, 11, required=True),
        Column("amount_paid", "Amount paid", A.NUM, 14, required=True),
        Column("making_charges", "Making charges", A.NUM, 13),
        Column("gst_paid", "GST paid", A.NUM, 11),
        Column("seller", "Seller", A.TEXT, 18),
        Column("rate_per_g", "Current rate / g", A.NUM, 13),
        Column("current_value", "Current value", A.NUM, 14),
        Column("value_date", "Value as of", A.DATE, 12),
        Column("sold_date", "Sold on", A.DATE, 12),
        Column("sold_amount", "Sold for", A.NUM, 13),
        Column("remarks", "Remarks", A.TEXT, 30),
    ),
    "Physical gold. 'Current rate / g' and 'Current value' are refreshed by Portfolio Tracker from the live "
    "gold price (before buy-back deductions and tax). Managed by Portfolio Tracker.",
)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6].upper()}"


class ExtraSheetsDAO:
    def __init__(self, protect_sheet: str = "Shares"):
        self.protect = protect_sheet

    # ------------------------------------------------------------------ read
    def read(self, data: bytes) -> Extras:
        wb = Workbook(data)
        warnings: list[str] = []
        trows, w = A.read_table(wb, TXN_SPEC)
        warnings += w
        txns = []
        for r in trows:
            t = self._txn(r, warnings)
            if t:
                txns.append(t)
        grows, w = A.read_table(wb, GOLD_SPEC)
        warnings += w
        gold = []
        for r in grows:
            g = self._gold(r, warnings)
            if g:
                gold.append(g)
        seen: set[str] = set()
        for x in txns + gold:  # ids must be unique; Excel copy/paste can duplicate them
            if x.id in seen:
                x.id = new_id("T" if isinstance(x, Transaction) else "G")
                warnings.append(f"Duplicate ID in row {x.row}; a new ID was assigned")
            seen.add(x.id)
        return Extras(txns, gold, warnings)

    @staticmethod
    def _txn(r: dict, warnings: list[str]) -> Transaction | None:
        row = r["_row"]
        try:
            typ = TxnType(str(r.get("type") or "").strip().upper())
        except ValueError:
            warnings.append(f"Transactions row {row}: unknown type '{r.get('type')}' (use SELL, BONUS or SPLIT); ignored")
            return None
        if not r.get("date") or not r.get("name"):
            warnings.append(f"Transactions row {row}: date and security are required; ignored")
            return None
        qty = r.get("qty") or 0.0
        if typ == TxnType.SELL and (qty <= 0 or not r.get("price")):
            warnings.append(f"Transactions row {row}: a sell needs a quantity and a price; ignored")
            return None
        if typ == TxnType.BONUS and qty <= 0:
            warnings.append(f"Transactions row {row}: a bonus needs the number of shares received; ignored")
            return None
        if typ == TxnType.SPLIT and not (r.get("ratio") and r["ratio"] > 0):
            warnings.append(f"Transactions row {row}: a split needs a ratio (new shares per old share); ignored")
            return None
        return Transaction(
            id=r.get("id") or new_id("T"), date=r["date"], type=typ, name=r["name"], qty=qty,
            price=r.get("price"), charges=r.get("charges") or 0.0, stt=r.get("stt") or 0.0, ratio=r.get("ratio"),
            isin=r.get("isin"), asset_class=(r.get("asset_class") or "share").lower(), broker=r.get("broker"),
            note_no=r.get("note_no"), source=r.get("source") or "manual", remarks=r.get("remarks"), row=row,
        )

    @staticmethod
    def _gold(r: dict, warnings: list[str]) -> GoldItem | None:
        row = r["_row"]
        if not r.get("date") or not r.get("weight_g") or r.get("amount_paid") is None:
            warnings.append(f"Gold row {row}: purchase date, weight and amount paid are required; ignored")
            return None
        return GoldItem(
            id=r.get("id") or new_id("G"), date=r["date"], description=r.get("description") or "Gold",
            form=(r.get("form") or "jewellery").lower(), purity=(r.get("purity") or "22K").upper(),
            weight_g=r["weight_g"], amount_paid=r["amount_paid"], making_charges=r.get("making_charges") or 0.0,
            gst_paid=r.get("gst_paid") or 0.0, seller=r.get("seller"), sold_date=r.get("sold_date"),
            sold_amount=r.get("sold_amount"), remarks=r.get("remarks"), rate_per_g=r.get("rate_per_g"),
            current_value=r.get("current_value"), value_date=r.get("value_date"), row=row,
        )

    # ----------------------------------------------------------------- write
    def write(self, data: bytes, *, transactions: list[Transaction] | None = None,
              gold: list[GoldItem] | None = None) -> bytes:
        """Rewrite the given app sheet(s). ``None`` = leave that sheet as it is."""
        tables = []
        if transactions is not None:
            rows = []
            for t in sorted(transactions, key=lambda x: (x.date, x.id)):
                rows.append({
                    "id": t.id, "date": t.date, "type": t.type.value, "name": t.name,
                    "qty": t.qty if t.type != TxnType.SPLIT else None, "price": t.price,
                    "value": t.proceeds if t.type == TxnType.SELL else None,
                    "charges": t.charges or None, "stt": t.stt or None, "ratio": t.ratio, "isin": t.isin,
                    "asset_class": t.asset_class, "broker": t.broker, "note_no": t.note_no, "source": t.source,
                    "remarks": t.remarks,
                })
            tables.append((TXN_SPEC, rows))
        if gold is not None:
            rows = [{
                "id": g.id, "date": g.date, "description": g.description, "form": g.form, "purity": g.purity,
                "weight_g": g.weight_g, "amount_paid": g.amount_paid, "making_charges": g.making_charges or None,
                "gst_paid": g.gst_paid or None, "seller": g.seller, "rate_per_g": g.rate_per_g,
                "current_value": g.current_value, "value_date": g.value_date, "sold_date": g.sold_date,
                "sold_amount": g.sold_amount, "remarks": g.remarks,
            } for g in sorted(gold, key=lambda x: (x.date, x.id))]
            tables.append((GOLD_SPEC, rows))
        if not tables:
            return data
        return A.write_tables(data, tables, protect=self.protect)


def today() -> dt.date:
    return dt.date.today()
