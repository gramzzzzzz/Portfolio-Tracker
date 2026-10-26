"""Security resolution and transaction-level de-duplication (pure functions).

Dedup works against the *sheet* (the source of truth), so it catches transactions that were
typed in manually as well as ones imported earlier, and it naturally "forgets" rows the
user deleted in Excel.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from app.domain.models import Lot, Side, Trade

_STOP = {"ltd", "limited", "the", "co", "company", "corp", "corporation", "inc", "pvt", "private", "india", "of", "and"}

PRICE_TOL = 0.005  # 0.5 %


def clean_name(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    return " ".join(t for t in s.split() if t not in _STOP)


def name_similarity(a: str, b: str) -> float:
    ca, cb = clean_name(a), clean_name(b)
    if not ca or not cb:
        return 0.0
    if ca == cb:
        return 1.0
    ta, tb = set(ca.split()), set(cb.split())
    # one name fully contained in the other ("ITC" vs "ITC LTD", "TATA MOTORS" vs "TATA MOTORS PASS VEH")
    if ta <= tb or tb <= ta:
        return 0.9
    jacc = len(ta & tb) / len(ta | tb)
    return max(jacc, difflib.SequenceMatcher(None, ca, cb).ratio())


@dataclass
class Resolution:
    sheet_name: str | None  # existing sheet name, or None for a new security
    confidence: float
    reason: str
    candidates: list[dict] = field(default_factory=list)


def resolve_security(trade: Trade, names: list[str], alias: dict | None, lots: list[Lot]) -> Resolution:
    """Map a broker security to a sheet security name."""
    if alias and alias.get("sheet_name"):
        exists = alias["sheet_name"] in names
        return Resolution(alias["sheet_name"], 1.0 if exists else 0.95,
                          "remembered from a previous import" + ("" if exists else " (not currently in sheet)"))
    for l in lots:
        if l.code and l.code.strip().upper() in (trade.isin.upper(),):
            return Resolution(l.name, 1.0, "ISIN in Code column")
    scored = sorted(((name_similarity(trade.security_name, n), n) for n in names), reverse=True)
    cands = [{"name": n, "score": round(s, 2)} for s, n in scored[:5] if s > 0.3]
    if scored and scored[0][0] >= 0.85:
        return Resolution(scored[0][1], scored[0][0], "name match", cands)
    return Resolution(None, 0.0, "new security", cands)


@dataclass
class DedupResult:
    status: str  # new | duplicate | possible_duplicate | sell
    reason: str
    matched_rows: list[int] = field(default_factory=list)


def _price_close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.011, PRICE_TOL * max(abs(a), abs(b)))


def classify_trades(items: list[tuple[str, Trade, str | None]], lots: list[Lot]) -> dict[str, DedupResult]:
    """items: (item_id, trade, resolved sheet name or None). Each sheet row can absorb at most
    one trade. Exact matches are consumed first so a possible match cannot steal them."""
    out: dict[str, DedupResult] = {}
    used: set[int] = set()
    by_name: dict[str, list[Lot]] = {}
    for l in lots:
        by_name.setdefault(l.name.casefold(), []).append(l)

    def candidates(trade, sheet_name):
        if not sheet_name or trade.trade_date is None:
            return []
        return [l for l in by_name.get(sheet_name.casefold(), []) if l.date == trade.trade_date and l.row not in used]

    # pass 1: exact
    for iid, t, nm in items:
        if t.side == Side.SELL:
            continue  # sells are classified against the Transactions sheet (classify_sells)
        for l in candidates(t, nm):
            if abs(l.qty - t.qty) < 1e-6 and (
                _price_close(l.rate, t.rate) or (l.qty and _price_close(l.cost / l.qty, t.net_value / t.qty))
            ):
                used.add(l.row)
                out[iid] = DedupResult("duplicate", f"Already in sheet (row {l.row}: {l.qty:g} @ {l.rate:,.2f})", [l.row])
                break
    # pass 2: partial
    for iid, t, nm in items:
        if iid in out or t.side == Side.SELL:
            continue
        cands = candidates(t, nm)
        same_qty = [l for l in cands if abs(l.qty - t.qty) < 1e-6]
        if same_qty:
            l = same_qty[0]
            used.add(l.row)
            out[iid] = DedupResult("possible_duplicate",
                                   f"Row {l.row} has the same date and quantity but rate {l.rate:,.2f} vs {t.rate:,.2f}",
                                   [l.row])
        elif cands:
            out[iid] = DedupResult("possible_duplicate",
                                   "A purchase on the same date exists with a different quantity (rows "
                                   + ", ".join(str(l.row) for l in cands) + ")", [l.row for l in cands])
        else:
            out[iid] = DedupResult("new", "Not found in sheet")
    return out


def classify_sells(items: list[tuple[str, Trade, str | None]], recorded: list, held: dict[str, float]) -> dict[str, DedupResult]:
    """Sells vs the app-owned Transactions sheet (rows typed by hand count too).

    ``recorded``: Transaction rows of type SELL; ``held``: units currently held per sheet name
    (casefolded). Each recorded row absorbs at most one trade."""
    out: dict[str, DedupResult] = {}
    used: set[str] = set()
    left = dict(held)
    for iid, t, nm in items:
        if t.side != Side.SELL:
            continue
        if not nm:
            out[iid] = DedupResult("sell_unknown", "No holding with this name in the sheet; choose which holding was sold")
            continue
        k = nm.casefold()
        same = [r for r in recorded if r.id not in used and r.name.casefold() == k and r.date == t.trade_date]
        exact = [r for r in same if abs(r.qty - t.qty) < 1e-6 and r.price and _price_close(r.price, t.rate)]
        if exact:
            used.add(exact[0].id)
            out[iid] = DedupResult("duplicate", f"Already in the Transactions sheet ({exact[0].id}: {exact[0].qty:g} @ "
                                                f"{exact[0].price:,.2f})")
            continue
        if same:
            used.add(same[0].id)
            out[iid] = DedupResult("possible_duplicate", f"A sale of {nm} on the same date is already recorded "
                                                         f"({same[0].id}: {same[0].qty:g} units)")
            continue
        have = left.get(k, 0.0)
        if t.qty > have + 1e-6:
            out[iid] = DedupResult("sell_exceeds", f"Sells {t.qty:g} units but only {have:g} are held in the sheet "
                                                   "(missing purchase rows?)")
            continue
        left[k] = have - t.qty
        out[iid] = DedupResult("sell", f"New sale - will be added to the Transactions sheet ({have:g} units held)")
    return out
