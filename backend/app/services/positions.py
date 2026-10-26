"""Positions engine: applies sells, bonus shares and splits (from the ``Transactions`` sheet)
to the purchase rows of the tracking sheet. Pure functions, no I/O.

Rules (Indian practice):
* **SELL** consumes units first-in-first-out per security (the depository debits the oldest
  units first). Each consumed slice keeps its own purchase date and cost.
* **BONUS** creates a new lot dated on the allotment date with **zero cost** (tax cost of
  bonus shares is nil and their holding period starts on allotment).
* **SPLIT** multiplies the units of every open lot by the ratio and divides the per-unit cost;
  purchase dates (holding period) are unchanged.
* Events are applied in date order; on the same day: purchases, then bonus, then split,
  then sells.

The original purchase rows are never modified; the engine returns adjusted *copies*
(remaining units and cost) that the analytics and tax code consume.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import re
from dataclasses import dataclass, field

from app.domain.models import Lot, Transaction, TxnType

BONUS_ROW_BASE = 1_000_000  # synthetic row keys for bonus lots (never collide with sheet rows)
_ORDER = {"purchase": 0, TxnType.BONUS: 1, TxnType.SPLIT: 2, TxnType.SELL: 3}


def key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).casefold()


@dataclass
class SaleLeg:
    lot_row: int
    origin: str  # purchase | bonus
    purchase_date: dt.date
    units: float
    sheet_cost: float  # share of the row's Total Purchase Price (incl. charges, STT)
    orig_qty: float | None  # for STT lookup of the source purchase row
    orig_cost: float | None
    orig_charges: float | None = None


@dataclass
class Sale:
    txn: Transaction
    name: str  # sheet name of the security
    legs: list[SaleLeg]
    unmatched_units: float = 0.0  # sold more than held -> reported as an issue

    @property
    def units(self) -> float:
        return sum(l.units for l in self.legs)


@dataclass
class Positions:
    open_lots: list[Lot]
    sales: list[Sale]
    issues: list[dict] = field(default_factory=list)
    adjusted_names: set[str] = field(default_factory=set)  # securities touched by transactions

    def held(self, name: str) -> float:
        k = key(name)
        return sum(l.held_qty for l in self.open_lots if key(l.name) == k)


def build(lots: list[Lot], txns: list[Transaction], *, as_of: dt.date | None = None) -> Positions:
    """Apply ``txns`` to ``lots``. With ``as_of``, only purchases and events on or before that
    date are considered (used for 'holdings on a date')."""
    lots = [l for l in lots if as_of is None or (l.date and l.date <= as_of)]
    txns = [t for t in txns if as_of is None or t.date <= as_of]
    by_name: dict[str, list[Lot]] = {}
    for l in lots:
        c = dataclasses.replace(l, orig_qty=l.qty if l.orig_qty is None else l.orig_qty,
                                orig_cost=l.cost if l.orig_cost is None else l.orig_cost,
                                orig_charges=l.charges if l.orig_charges is None else l.orig_charges)
        by_name.setdefault(key(l.name), []).append(c)
    issues: list[dict] = []
    sales: list[Sale] = []
    adjusted: set[str] = set()
    bonus_n = 0

    events: list[tuple] = []
    for k, ls in by_name.items():
        for l in ls:
            events.append((l.date or dt.date.min, _ORDER["purchase"], l.row, "purchase", k, l))
    for i, t in enumerate(txns):
        k = key(t.name)
        if k not in by_name:
            issues.append({"severity": "warning", "code": "txn_unknown_security", "txn": t.id,
                           "message": f"{t.type.value} on {t.date:%d %b %Y}: '{t.name}' has no purchase rows in the sheet "
                                      f"(name must match the main sheet exactly); ignored"})
            continue
        events.append((t.date, _ORDER[t.type], i, "txn", k, t))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    open_by: dict[str, list[Lot]] = {}
    for date, _, _, kind, k, obj in events:
        book = open_by.setdefault(k, [])
        if kind == "purchase":
            book.append(obj)
            continue
        t: Transaction = obj
        adjusted.add(k)
        if t.type == TxnType.BONUS:
            src = by_name[k][-1]
            bonus_n += 1
            book.append(dataclasses.replace(
                src, row=BONUS_ROW_BASE + bonus_n, date=t.date, qty=t.qty, rate=0.0, charges=0.0, cost=0.0,
                held_qty=t.qty, origin="bonus", txn_id=t.id, orig_qty=None, orig_cost=None, orig_charges=None))
        elif t.type == TxnType.SPLIT:
            r = float(t.ratio)
            for l in book:
                if l.held_qty > 0:
                    l.held_qty *= r
                    l.qty *= r
                    l.rate /= r
                    if l.market_rate:
                        l.market_rate /= r
            # market rates written to the sheet after the split are already post-split
        elif t.type == TxnType.SELL:
            left = t.qty
            legs: list[SaleLeg] = []
            for l in sorted(book, key=lambda x: (x.date or dt.date.min, x.row)):
                if left <= 1e-9:
                    break
                if l.held_qty <= 1e-9:
                    continue
                u = min(l.held_qty, left)
                frac = u / l.held_qty
                legs.append(SaleLeg(l.row, l.origin, l.date, u, l.cost * frac, l.orig_qty, l.orig_cost, l.orig_charges))
                keep = 1 - frac
                l.cost *= keep
                l.charges *= keep
                l.qty *= keep
                l.held_qty -= u
                left -= u
            sale = Sale(t, by_name[k][0].name, legs, max(0.0, left))
            if sale.unmatched_units > 1e-6:
                issues.append({"severity": "error", "code": "sell_exceeds_holding", "txn": t.id,
                               "message": f"Sell of {t.qty:g} {sale.name} on {t.date:%d %b %Y} exceeds the "
                                          f"{t.qty - left:g} units held then; the extra {left:g} were ignored"})
            sales.append(sale)

    open_lots = [l for ls in open_by.values() for l in ls if l.held_qty > 1e-9]
    open_lots.sort(key=lambda l: (l.row if l.origin == "purchase" else 10**9, l.date or dt.date.min))
    # bonus already counted in the sheet's 'held quantity' column (e.g. K22 = E22+50) AND
    # recorded as a BONUS row would be counted twice -> warn
    for k in adjusted:
        sheet_extra = [l for l in lots if key(l.name) == k and abs(l.held_qty - l.qty) > 1e-9]
        if sheet_extra and any(t.type == TxnType.BONUS and key(t.name) == k for t in txns):
            issues.append({"severity": "warning", "code": "bonus_double_count",
                           "message": f"{sheet_extra[0].name}: the main sheet already adds extra shares in its "
                                      f"held-quantity column (row {sheet_extra[0].row}) and a BONUS is also recorded "
                                      f"in Transactions. Keep only one, or the bonus is counted twice."})
    return Positions(open_lots, sales, issues, adjusted)
