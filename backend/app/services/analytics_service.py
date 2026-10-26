"""Portfolio analytics computed from the sheet snapshot (+ optional live prices).

All numbers are derived from the sheet's input columns (qty, rate, charges, dates, held qty)
and valued at live prices. If a live quote is unavailable for a holding, its market rate in
the sheet is used - which is the last live price the price sync wrote there.
"""
from __future__ import annotations

import bisect
import datetime as dt
import math
from collections import defaultdict
from dataclasses import dataclass

from app.dao.repo.sqlite_repo import Repository
from app.domain.models import Lot, SheetSnapshot
from app.services.finance import add_months, fy_label
from app.services.finance import xirr as _raw_xirr
from app.services.prices_service import PriceService
from app.services.settings_service import SettingsService

MIN_XIRR_DAYS = 365  # annualising shorter periods produces misleading numbers


def xirr(flows):
    ds = [d for d, a in flows if d and a]
    if not ds or (max(ds) - min(ds)).days < MIN_XIRR_DAYS:
        return None
    return _raw_xirr(flows)


def _r(x, n=2):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(x, n)


def tax_class(sector: str | None, name: str, overrides: dict) -> str:
    if sector and sector in overrides:
        return overrides[sector]
    s = f"{sector or ''} {name}".lower()
    if any(k in s for k in ("gold", "silver")):
        return "non_equity"
    if any(k in s for k in ("debt", "liquid", "bond", "gilt")):
        return "debt"
    return "equity"


@dataclass
class LotView:
    lot: Lot
    price: float | None
    price_source: str
    value: float
    gain: float
    days: int | None
    term: str  # LT | ST | ?
    lt_date: dt.date | None
    tclass: str

    def as_dict(self):
        l = self.lot
        return {
            "row": l.row, "origin": l.origin, "name": l.name, "date": l.date.isoformat() if l.date else None, "qty": l.qty,
            "held_qty": l.held_qty, "rate": l.rate, "charges": _r(l.charges), "cost": _r(l.cost),
            "unit_cost": _r(l.cost / l.held_qty if l.held_qty else None, 4), "price": _r(self.price, 4),
            "price_source": self.price_source, "value": _r(self.value), "gain": _r(self.gain),
            "gain_pct": _r(self.gain / l.cost * 100 if l.cost else None), "days": self.days, "term": self.term,
            "lt_date": self.lt_date.isoformat() if self.lt_date else None, "tax_class": self.tclass,
            "sector": l.sector, "block": l.block, "code": l.code,
        }


class AnalyticsService:
    def __init__(self, repo: Repository, prices: PriceService, settings: SettingsService):
        self.repo = repo
        self.prices = prices
        self.settings = settings
        self.tax = None  # TaxService, attached by the container (post-tax figures)

    def attach_tax(self, tax) -> None:
        self.tax = tax

    portfolio = None  # PortfolioService (attached by the container)

    def attach_portfolio(self, portfolio) -> None:
        self.portfolio = portfolio

    def _realised_flows(self, name: str | None = None) -> list[tuple[dt.date, float]]:
        """Cash flows for XIRR that the open lots alone miss once something was sold: the full
        cost of every purchase row (negative) minus the open lots' remaining cost, and the
        net proceeds of each recorded sale (positive)."""
        if self.portfolio is None:
            return []
        try:
            st = self.portfolio.state()
        except Exception:
            return []
        if not st.positions.sales:
            return []
        k = None if name is None else name.casefold()
        flows = []
        for sale in st.positions.sales:
            if k and sale.name.casefold() != k:
                continue
            t = sale.txn
            flows.append((t.date, (t.price or 0) * sale.units - (t.charges or 0) - (t.stt or 0)))
            for leg in sale.legs:  # cost of the sold slices (open lots carry only what is left)
                flows.append((leg.purchase_date, -leg.sheet_cost))
        return flows

    # ------------------------------------------------------------- pricing
    def price_lots(self, snap: SheetSnapshot, today: dt.date | None = None):
        today = today or dt.date.today()
        cfg = self.settings.all()
        names = sorted({l.name for l in snap.lots})
        quotes, symbols = {}, {}
        if self.prices.provider is not None:
            codes = {}
            for l in snap.lots:
                codes.setdefault(l.name, l.code)
            symbols = self.prices.symbol_map(names, codes, auto_resolve=True)
            quotes = self.prices.quotes([s for s in symbols.values() if s])
        views = []
        for l in snap.lots:
            q = quotes.get(symbols.get(l.name) or "")
            if q:
                price, src = q["price"], "live"
            else:
                price, src = l.market_rate, "last_sync"
            value = (price or 0.0) * l.held_qty
            lt_date = add_months(l.date, int(cfg["equity_lt_months"])) if l.date else None
            days = (today - l.date).days if l.date else None
            term = "?" if not l.date else ("LT" if today > lt_date else "ST")
            views.append(LotView(l, price, src, value, value - l.cost, days, term, lt_date,
                                 tax_class(l.sector, l.name, cfg["tax_class_by_sector"])))
        return views, quotes, symbols

    # ------------------------------------------------------------- overview
    def overview(self, snap: SheetSnapshot) -> dict:
        today = dt.date.today()
        views, quotes, symbols = self.price_lots(snap, today)
        any_live = any(v.price_source == "live" for v in views)
        as_of = today if any_live else max([d for d in [snap.valuation_date] + [l.date for l in snap.lots] if d] or [today])
        invested = sum(v.lot.cost for v in views)
        value = sum(v.value for v in views)
        flows = [(v.lot.date, -v.lot.cost) for v in views if v.lot.date] + [(as_of, value)] + self._realised_flows()
        holdings = self._holdings(views, quotes, symbols, as_of, value)
        day_change = None
        if any_live:
            dc = 0.0
            for h in holdings:
                q = quotes.get(h["symbol"] or "")
                if q and q.get("prev_close"):
                    dc += (q["price"] - q["prev_close"]) * h["qty"]
            day_change = dc
        gross = sum(v.lot.qty * v.lot.rate for v in views)
        charges = sum(v.lot.charges for v in views)
        liq = self.tax.liquidation(snap, views) if (self.tax is not None and views) else None
        return {
            "meta": {
                "valuation_date": snap.valuation_date.isoformat() if snap.valuation_date else None,
                "price_source": "live" if any_live else "last_sync",
                "live_coverage": _r(sum(1 for h in holdings if h["price_source"] == "live") / len(holdings) * 100, 0) if holdings else None,
                "as_of": as_of.isoformat(),
                "sheet_name": snap.sheet_name,
            },
            "kpis": {
                "invested": _r(invested), "value": _r(value), "gain": _r(value - invested),
                "gain_pct": _r((value / invested - 1) * 100 if invested else None),
                "xirr": _r(xirr(flows) * 100 if xirr(flows) is not None else None),
                "day_change": _r(day_change),
                "day_change_pct": _r(day_change / (value - day_change) * 100 if day_change is not None and value - day_change else None),
                "holdings": len(holdings), "lots": len(views),
                "charges": _r(charges), "charges_pct": _r(charges / gross * 100 if gross else None, 3),
                "sheet_grand_total": _r(snap.totals.get("grand_total_cost")),
                **self._liquidation_kpis(liq, invested),
            },
            "holdings": self._with_tax(holdings, liq),
            "allocation": self._allocation(holdings, snap),
            "concentration": self._concentration(holdings),
            "timeline": self._timeline(snap.lots),
            "issues": [i.__dict__ for i in snap.issues],
        }

    def _liquidation_kpis(self, liq, invested) -> dict:
        if not liq:
            return {}
        t = liq["total"]
        return {"tax_if_sold": t["tax_if_sold"], "exit_costs": t["exit_costs"], "after_tax_value": t["after_tax_value"],
                "after_tax_gain": t["after_tax_gain"],
                "after_tax_gain_pct": _r(t["after_tax_gain"] / invested * 100 if invested else None)}

    @staticmethod
    def _with_tax(holdings, liq) -> list[dict]:
        for h in holdings:
            h.update((liq or {}).get("holdings", {}).get(h["name"], {}))
        return holdings

    def _holdings(self, views: list[LotView], quotes, symbols, as_of, total_value) -> list[dict]:
        groups: dict[str, list[LotView]] = defaultdict(list)
        for v in views:
            groups[v.lot.name].append(v)
        out = []
        for name, vs in groups.items():
            vs.sort(key=lambda v: (v.lot.date or dt.date.min, v.lot.row))
            qty = sum(v.lot.held_qty for v in vs)
            cost = sum(v.lot.cost for v in vs)
            value = sum(v.value for v in vs)
            sym = symbols.get(name)
            q = quotes.get(sym or "")
            price = vs[-1].price
            flows = [(v.lot.date, -v.lot.cost) for v in vs if v.lot.date] + [(as_of, value)] + self._realised_flows(name)
            x = xirr(flows)
            lt_qty = sum(v.lot.held_qty for v in vs if v.term == "LT")
            out.append({
                "name": name, "sector": vs[-1].lot.sector, "block": vs[-1].lot.block, "code": vs[-1].lot.code,
                "symbol": sym, "qty": qty, "avg_cost": _r(cost / qty if qty else None, 4), "invested": _r(cost),
                "price": _r(price, 4), "price_source": vs[-1].price_source, "value": _r(value),
                "gain": _r(value - cost), "gain_pct": _r((value / cost - 1) * 100 if cost else None),
                "xirr": _r(x * 100 if x is not None else None),
                "weight": _r(value / total_value * 100 if total_value else None),
                "lots": len(vs), "first_date": vs[0].lot.date.isoformat() if vs[0].lot.date else None,
                "last_date": vs[-1].lot.date.isoformat() if vs[-1].lot.date else None,
                "lt_qty": lt_qty, "st_qty": qty - lt_qty,
                "day_change_pct": _r(q.get("change_pct")) if q else None,
                "high52": q.get("high52") if q else None, "low52": q.get("low52") if q else None,
                "from_high_pct": _r((price / q["high52"] - 1) * 100) if q and q.get("high52") and price else None,
                "rows": [v.lot.row for v in vs if v.lot.origin == "purchase"],
                "lot_details": [v.as_dict() for v in vs],
            })
        out.sort(key=lambda h: -(h["value"] or 0))
        return out

    def _allocation(self, holdings, snap: SheetSnapshot) -> dict:
        by_sector: dict[str, dict] = defaultdict(lambda: {"value": 0.0, "invested": 0.0, "holdings": 0})
        by_block: dict[str, dict] = defaultdict(lambda: {"value": 0.0, "invested": 0.0})
        sec_title = {}
        for s in snap.sections:
            for b in s.blocks:
                sec_title[b.header] = s.title
        by_class: dict[str, dict] = defaultdict(lambda: {"value": 0.0, "invested": 0.0})
        total = sum(h["value"] or 0 for h in holdings) or 1.0
        for h in holdings:
            k = h["sector"] or "Unassigned"
            by_sector[k]["value"] += h["value"] or 0
            by_sector[k]["invested"] += h["invested"] or 0
            by_sector[k]["holdings"] += 1
            by_block[h["block"] or "-"]["value"] += h["value"] or 0
            by_block[h["block"] or "-"]["invested"] += h["invested"] or 0
            cls = "Stocks" if sec_title.get(h["block"]) == snap.sections[0].title else (h["block"] or "Other")
            by_class[cls]["value"] += h["value"] or 0
            by_class[cls]["invested"] += h["invested"] or 0

        def fmt(d):
            rows = [{"name": k, **{kk: _r(vv) for kk, vv in v.items()}, "weight": _r(v["value"] / total * 100)}
                    for k, v in d.items()]
            return sorted(rows, key=lambda x: -(x["value"] or 0))

        return {"sector": fmt(by_sector), "block": fmt(by_block), "asset_class": fmt(by_class)}

    def _concentration(self, holdings) -> dict:
        cfg = self.settings.all()
        total = sum(h["value"] or 0 for h in holdings)
        if not total:
            return {"top1": None, "top5": None, "hhi": None, "effective_n": None, "alerts": []}
        w = sorted(((h["value"] or 0) / total for h in holdings), reverse=True)
        hhi = sum(x * x for x in w)
        alerts = []
        for h in holdings:
            if (h["weight"] or 0) > cfg["max_holding_pct"]:
                alerts.append({"type": "holding", "name": h["name"], "weight": h["weight"],
                               "limit": cfg["max_holding_pct"]})
        sec = defaultdict(float)
        for h in holdings:
            sec[h["sector"] or "Unassigned"] += (h["value"] or 0) / total * 100
        for s, wt in sec.items():
            if wt > cfg["max_sector_pct"]:
                alerts.append({"type": "sector", "name": s, "weight": _r(wt), "limit": cfg["max_sector_pct"]})
        return {"top1": _r(w[0] * 100), "top5": _r(sum(w[:5]) * 100), "hhi": _r(hhi, 4),
                "effective_n": _r(1 / hhi, 1) if hhi else None, "alerts": alerts}

    def _timeline(self, lots: list[Lot]) -> list[dict]:
        months: dict[str, float] = defaultdict(float)
        counts: dict[str, int] = defaultdict(int)
        for l in lots:
            if l.date:
                k = l.date.strftime("%Y-%m")
                months[k] += l.cost
                counts[k] += 1
        out, cum = [], 0.0
        for k in sorted(months):
            cum += months[k]
            out.append({"month": k, "invested": _r(months[k]), "cumulative": _r(cum), "buys": counts[k]})
        return out

    # -------------------------------------------------------------- charges
    def charges(self, snap: SheetSnapshot) -> dict:
        lots = snap.lots
        buckets = [(0, 10000, "< ₹10k"), (10000, 50000, "₹10k–50k"), (50000, 100000, "₹50k–1L"),
                   (100000, float("inf"), "> ₹1L")]
        rows = []
        for lo, hi, label in buckets:
            sel = [l for l in lots if lo <= l.qty * l.rate < hi]
            g = sum(l.qty * l.rate for l in sel)
            c = sum(l.charges for l in sel)
            rows.append({"bucket": label, "orders": len(sel), "gross": _r(g), "charges": _r(c),
                         "charges_pct": _r(c / g * 100 if g else None, 3)})
        notes = self.repo.notes()
        breakdown = defaultdict(float)
        for n in notes:
            for k, v in n["charges"].items():
                breakdown[k] += v or 0
        by_year = defaultdict(lambda: {"gross": 0.0, "charges": 0.0})
        for l in lots:
            if l.date:
                k = fy_label(l.date)
                by_year[k]["gross"] += l.qty * l.rate
                by_year[k]["charges"] += l.charges
        gross = sum(l.qty * l.rate for l in lots)
        total = sum(l.charges for l in lots)
        return {
            "total": _r(total), "gross": _r(gross), "pct": _r(total / gross * 100 if gross else None, 3),
            "by_size": rows,
            "by_fy": [{"fy": k, "gross": _r(v["gross"]), "charges": _r(v["charges"]),
                       "pct": _r(v["charges"] / v["gross"] * 100 if v["gross"] else None, 3)}
                      for k, v in sorted(by_year.items())],
            "breakdown_imported": {k: _r(v) for k, v in breakdown.items() if v},
            "notes": [{"broker": n["broker"], "note_no": n["note_no"], "trade_date": n["trade_date"],
                       "net_payable": n["net_payable"], "charges_total": _r(sum(v or 0 for v in n["charges"].values()))}
                      for n in notes[:50]],
        }

    # ------------------------------------------------------------ benchmark
    def benchmark(self, snap: SheetSnapshot) -> dict:
        cfg = self.settings.all()
        sym = cfg["benchmark_symbol"]
        lots = [l for l in snap.lots if l.date]
        if not lots or self.prices.provider is None:
            return {"available": False, "reason": "Live prices are disabled" if lots else "No dated purchases"}
        start = min(l.date for l in lots)
        hist = self.prices.history(sym, start)
        if not hist:
            return {"available": False, "reason": f"Could not download {sym} history"}
        dates = [d for d, _ in hist]

        def close_on(d):
            i = bisect.bisect_right(dates, d) - 1
            return hist[max(i, 0)][1]

        today = dt.date.today()
        last = hist[-1][1]
        units = 0.0
        flows = []
        for l in lots:
            units += l.cost / close_on(l.date)
            flows.append((l.date, -l.cost))
        bench_value = units * last
        ov = self.overview(snap)
        port_value = ov["kpis"]["value"] or 0
        bx = xirr(flows + [(today, bench_value)])
        px = xirr(flows + [(today, port_value)])

        # monthly series: invested, benchmark value (same cash flows)
        series = []
        lots_sorted = sorted(lots, key=lambda l: l.date)
        d = dt.date(start.year, start.month, 1)
        while d <= today:
            nxt = add_months(d, 1)
            point = min(nxt - dt.timedelta(days=1), today)
            inv = sum(l.cost for l in lots_sorted if l.date <= point)
            u = sum(l.cost / close_on(l.date) for l in lots_sorted if l.date <= point)
            series.append({"date": point.isoformat(), "invested": _r(inv), "benchmark": _r(u * close_on(point))})
            d = nxt
        if series:
            series[-1]["portfolio"] = _r(port_value)
        return {
            "available": True, "symbol": sym, "benchmark_value": _r(bench_value), "portfolio_value": _r(port_value),
            "invested": _r(sum(l.cost for l in lots)),
            "benchmark_xirr": _r(bx * 100 if bx is not None else None),
            "portfolio_xirr": _r(px * 100 if px is not None else None),
            "alpha": _r((px - bx) * 100) if px is not None and bx is not None else None,
            "price_source": ov["meta"]["price_source"], "series": series,
        }
