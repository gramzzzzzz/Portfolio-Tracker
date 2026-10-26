"""Investment history across every asset class.

* ``events()`` - one chronological list of everything that happened: share/ETF purchases
  (main sheet), sells / bonus shares / splits (Transactions sheet), physical gold bought and
  sold (Gold sheet). Mutual funds, FDs and RDs are part of the model (``AssetClass``) and of
  the response (``asset_classes`` with ``supported: false``) so they can be added later by
  writing one source adapter each, without changing the API or the UI.
* ``holdings_on(date)`` - what was held on a date: units and cost per holding, computed by
  replaying all events up to that date.
* ``timeline()`` - month-end invested amount (cost of what was held) per asset class, plus
  money put in and taken out each month.

Values are at cost: historical market prices are not stored. Today's value is on the
dashboard.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from app.domain.models import AssetClass, TxnType
from app.services import positions as P
from app.services.finance import add_months
from app.services.portfolio_service import PortfolioService

ASSET_CLASSES = [
    {"id": AssetClass.SHARE.value, "label": "Shares", "supported": True, "source": "Main sheet + Transactions sheet"},
    {"id": AssetClass.ETF.value, "label": "ETFs", "supported": True, "source": "Main sheet + Transactions sheet"},
    {"id": AssetClass.GOLD.value, "label": "Physical gold", "supported": True, "source": "Gold sheet"},
    {"id": AssetClass.MUTUAL_FUND.value, "label": "Mutual funds", "supported": False,
     "source": "Planned (the main sheet has a Mutual Fund section; units are read from it today)"},
    {"id": AssetClass.FD.value, "label": "Fixed deposits", "supported": False, "source": "Planned"},
    {"id": AssetClass.RD.value, "label": "Recurring deposits", "supported": False, "source": "Planned"},
]


def _r(x, n=2):
    return None if x is None else round(x, n) + 0.0


class HistoryService:
    def __init__(self, portfolio: PortfolioService):
        self.portfolio = portfolio

    # ------------------------------------------------------------- classify
    @staticmethod
    def _class_of(lot, sections) -> str:
        """Asset class of a main-sheet lot from the section it sits in."""
        title = (sections[lot.section].title or "").lower() if lot.section < len(sections) else ""
        if "mutual" in title or (lot.block or "").lower().startswith("mutual"):
            return AssetClass.MUTUAL_FUND.value
        if "etf" in title or "etf" in (lot.block or "").lower():
            return AssetClass.ETF.value
        return AssetClass.SHARE.value

    # --------------------------------------------------------------- events
    def events(self) -> dict:
        st = self.portfolio.state()
        sheet = st.sheet
        cls = {l.name.casefold(): self._class_of(l, sheet.sections) for l in sheet.lots}
        out = []
        for l in sheet.lots:
            out.append({
                "date": l.date.isoformat() if l.date else None, "type": "BUY", "asset_class": cls[l.name.casefold()],
                "name": l.name, "units": l.held_qty, "price": l.rate, "amount": -_r(l.cost), "charges": _r(l.charges),
                "source": "Main sheet", "ref": f"row {l.row}", "sector": l.sector,
            })
        sales = {s.txn.id: s for s in st.positions.sales}
        for t in st.extras.transactions:
            e = {"date": t.date.isoformat(), "type": t.type.value, "name": t.name,
                 "asset_class": cls.get(t.name.casefold(), t.asset_class), "source": "Transactions sheet",
                 "ref": t.id + (f" · note {t.note_no}" if t.note_no else ""), "remarks": t.remarks}
            if t.type == TxnType.SELL:
                s = sales.get(t.id)
                cost = sum(l.sheet_cost for l in s.legs) if s else None
                net = t.proceeds - (t.charges or 0) - (t.stt or 0)
                e.update(units=-t.qty, price=t.price, amount=_r(net), charges=_r((t.charges or 0) + (t.stt or 0)),
                         cost_of_units=_r(cost), gain=_r(net - cost) if cost is not None else None)
            elif t.type == TxnType.BONUS:
                e.update(units=t.qty, price=0.0, amount=0.0)
            else:
                e.update(ratio=t.ratio, amount=0.0)
            out.append(e)
        for g in st.extras.gold:
            label = f"{g.description} ({g.weight_g:g} g {g.purity} {g.form})"
            out.append({"date": g.date.isoformat(), "type": "BUY", "asset_class": AssetClass.GOLD.value, "name": label,
                        "units": g.weight_g, "unit": "g", "amount": -_r(g.amount_paid), "charges": _r(g.making_charges + g.gst_paid),
                        "source": "Gold sheet", "ref": g.id})
            if g.sold_date:
                out.append({"date": g.sold_date.isoformat(), "type": "SELL", "asset_class": AssetClass.GOLD.value,
                            "name": label, "units": -g.weight_g, "unit": "g", "amount": _r(g.sold_amount),
                            "gain": _r((g.sold_amount or 0) - g.amount_paid), "source": "Gold sheet", "ref": g.id})
        out.sort(key=lambda e: (e["date"] or "", 0 if e["type"] == "BUY" else 1), reverse=True)
        counts = defaultdict(int)
        for e in out:
            counts[e["asset_class"]] += 1
        classes = [dict(c, events=counts.get(c["id"], 0)) for c in ASSET_CLASSES]
        return {"events": out, "asset_classes": classes, "issues": st.positions.issues,
                "first_date": min((e["date"] for e in out if e["date"]), default=None)}

    # ------------------------------------------------------------- holdings
    def holdings_on(self, on: dt.date) -> dict:
        st = self.portfolio.state()
        sheet = st.sheet
        pos = P.build(sheet.lots, st.extras.transactions, as_of=on)
        groups: dict[str, dict] = {}
        for l in pos.open_lots:
            g = groups.setdefault(l.name, {"name": l.name, "asset_class": self._class_of(l, sheet.sections),
                                           "sector": l.sector, "units": 0.0, "cost": 0.0, "lots": 0, "bonus_units": 0.0})
            g["units"] += l.held_qty
            g["cost"] += l.cost
            g["lots"] += 1
            if l.origin == "bonus":
                g["bonus_units"] += l.held_qty
        rows = [dict(g, units=_r(g["units"], 4), cost=_r(g["cost"]), bonus_units=_r(g["bonus_units"], 4),
                     avg_cost=_r(g["cost"] / g["units"], 4) if g["units"] else None) for g in groups.values()]
        for g in st.extras.gold:
            if g.date <= on and not (g.sold_date and g.sold_date <= on):
                rows.append({"name": f"{g.description} ({g.purity} {g.form})", "asset_class": AssetClass.GOLD.value,
                             "sector": "Physical gold", "units": g.weight_g, "unit": "g", "cost": _r(g.amount_paid),
                             "lots": 1, "avg_cost": _r(g.amount_paid / g.weight_g) if g.weight_g else None})
        rows.sort(key=lambda r: -(r["cost"] or 0))
        by_class = defaultdict(float)
        for r in rows:
            by_class[r["asset_class"]] += r["cost"] or 0
        return {"date": on.isoformat(), "holdings": rows, "total_cost": _r(sum(by_class.values())),
                "by_class": {k: _r(v) for k, v in by_class.items()}}

    # ------------------------------------------------------------- timeline
    def timeline(self) -> dict:
        st = self.portfolio.state()
        dates = [l.date for l in st.sheet.lots if l.date] + [t.date for t in st.extras.transactions] + \
                [g.date for g in st.extras.gold]
        if not dates:
            return {"points": []}
        today = dt.date.today()
        d = dt.date(min(dates).year, min(dates).month, 1)
        ev = self.events()["events"]
        flow_in, flow_out = defaultdict(float), defaultdict(float)
        for e in ev:
            if not e["date"]:
                continue
            m = e["date"][:7]
            if (e.get("amount") or 0) < 0:
                flow_in[m] += -e["amount"]
            elif (e.get("amount") or 0) > 0:
                flow_out[m] += e["amount"]
        points = []
        while d <= today:
            end = min(add_months(d, 1) - dt.timedelta(days=1), today)
            h = self.holdings_on(end)
            m = d.strftime("%Y-%m")
            points.append({"month": m, "date": end.isoformat(), "total_cost": h["total_cost"], **h["by_class"],
                           "invested": _r(flow_in.get(m, 0.0)), "withdrawn": _r(flow_out.get(m, 0.0))})
            d = add_months(d, 1)
        classes = sorted({k for p in points for k in p if k in {c["id"] for c in ASSET_CLASSES}})
        for p in points:  # every class in every point (charts need continuous series)
            for k in classes:
                p.setdefault(k, 0.0)
        return {"points": points, "classes": classes}
