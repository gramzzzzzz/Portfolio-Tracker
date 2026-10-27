"""Performance metrics that are meaningful before a year of history (XIRR needs >= 1 year).

Built from a *daily value series*: for every trading day since the first purchase, the units
held that day (main sheet + sells / bonus / splits from the Transactions sheet, via the
positions engine) are valued at that day's closing price (Yahoo daily history, cached).

Metrics (all pre-tax; shares + ETFs, i.e. the main sheet - physical gold has its own page):

* **Total P&L** = unrealised (today's value - cost of what is held) + realised (net sale
  proceeds - cost of the units sold).
* **Return on invested money** (Modified Dietz) for a period: gain / (value at start +
  time-weighted cash flows). It answers "what did my money earn over this period", counting
  money invested mid-period only for the time it was invested. Not annualised.
* **Time-weighted return (TWR)**: chains daily returns so the timing and size of your
  deposits don't affect it - "how did my picks perform", directly comparable with an index.
* **Same-money benchmark**: every rupee you put in (and took out) invested in the benchmark
  index on the same dates; its value today vs yours.
* **Max drawdown**: largest fall from a peak of the TWR growth index.
* **Best / worst contributors**: total P&L per holding, realised included.

Annualised figures are shown only for periods of at least 365 days.
"""
from __future__ import annotations

import bisect
import datetime as dt
import math
from collections import defaultdict

from app.services import positions as P
from app.services.analytics_service import xirr
from app.services.finance import add_months, fy_bounds


def _r(x, n=2):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(x, n) + 0.0


class _Series:
    """Daily closes with as-of lookup."""

    def __init__(self, points: list[tuple[dt.date, float]]):
        pts = sorted((d, p) for d, p in points if p)
        self.dates = [d for d, _ in pts]
        self.closes = [p for _, p in pts]

    def on(self, d: dt.date) -> float | None:
        i = bisect.bisect_right(self.dates, d) - 1
        return self.closes[i] if i >= 0 else None

    def __bool__(self):
        return bool(self.dates)


def modified_dietz(v0: float, v1: float, flows: list[tuple[dt.date, float]], start: dt.date, end: dt.date) -> float | None:
    """flows: (date, amount) with + = money put in, - = money taken out, in (start, end]."""
    span = (end - start).days
    if span <= 0:
        return None
    net = sum(a for _, a in flows)
    weighted = sum(a * (end - d).days / span for d, a in flows)
    base = v0 + weighted
    if base <= 1e-9:
        return None
    return (v1 - v0 - net) / base


def max_drawdown(index: list[tuple[dt.date, float]]) -> dict | None:
    peak, peak_d, worst = None, None, (0.0, None, None)
    for d, v in index:
        if peak is None or v > peak:
            peak, peak_d = v, d
        dd = v / peak - 1
        if dd < worst[0]:
            worst = (dd, peak_d, d)
    if worst[1] is None:
        return {"pct": 0.0, "peak": None, "trough": None}
    return {"pct": _r(worst[0] * 100), "peak": worst[1].isoformat(), "trough": worst[2].isoformat()}


class PerformanceService:
    def __init__(self, portfolio, prices, analytics, settings):
        self.portfolio = portfolio
        self.prices = prices
        self.analytics = analytics
        self.settings = settings
        self._cache: tuple[tuple, dict] | None = None

    # ------------------------------------------------------------------ inputs
    def _flows(self, st) -> list[tuple[dt.date, float, str]]:
        """(date, amount, name): purchases +cost, sales -(net proceeds)."""
        out = [(l.date, l.cost, l.name) for l in st.sheet.lots if l.date]
        for s in st.positions.sales:
            t = s.txn
            out.append((t.date, -((t.price or 0) * s.units - (t.charges or 0) - (t.stt or 0)), s.name))
        return out

    def compute(self) -> dict:
        st = self.portfolio.state()
        today = dt.date.today()
        head = self.portfolio.versions.head()
        key = (head["id"], head["sha256"], today, self.settings.get("benchmark_symbol"))
        if self._cache and self._cache[0] == key:
            return self._cache[1]
        out = self._compute(st, today)
        self._cache = (key, out)
        return out

    def _compute(self, st, today: dt.date) -> dict:
        cfg = self.settings.all()
        flows = self._flows(st)
        ov = self.analytics.overview(st.snap)
        value_now = ov["kpis"]["value"] or 0.0
        invested_now = ov["kpis"]["invested"] or 0.0
        unreal = value_now - invested_now
        realised_by = defaultdict(float)
        for s in st.positions.sales:
            t = s.txn
            realised_by[s.name] += (t.price or 0) * s.units - (t.charges or 0) - (t.stt or 0) - sum(l.sheet_cost for l in s.legs)
        realised = sum(realised_by.values())
        total_in = sum(a for _, a, _ in flows if a > 0)
        total_out = -sum(a for _, a, _ in flows if a < 0)
        if not flows:
            return {"available": False, "reason": "No purchases yet"}
        start = min(d for d, _, _ in flows)
        span_days = (today - start).days

        # since inception (needs only cash flows and today's value)
        md_all = modified_dietz(0.0, value_now, [(d, a) for d, a, _ in flows], start, today)
        x = xirr([(d, -a) for d, a, _ in flows] + [(today, value_now)])
        summary = {
            "total_pnl": _r(unreal + realised), "unrealised": _r(unreal), "realised": _r(realised),
            "total_invested": _r(total_in), "total_withdrawn": _r(total_out), "value": _r(value_now),
            "simple_return_pct": _r((unreal + realised) / total_in * 100) if total_in else None,
            "md_since_start_pct": _r(md_all * 100) if md_all is not None else None,
            "xirr_pct": _r(x * 100) if x is not None else None,
            "days": span_days, "start": start.isoformat(),
        }
        contributors = []
        for h in ov["holdings"]:
            contributors.append({"name": h["name"], "unrealised": h["gain"], "realised": _r(realised_by.get(h["name"], 0.0)),
                                 "total": _r((h["gain"] or 0) + realised_by.get(h["name"], 0.0)), "value": h["value"]})
        for name, r in realised_by.items():
            if not any(c["name"] == name for c in contributors):
                contributors.append({"name": name, "unrealised": 0.0, "realised": _r(r), "total": _r(r), "value": 0.0})
        contributors.sort(key=lambda c: -(c["total"] or 0))
        base = {"available": True, "summary": summary, "contributors": contributors,
                "benchmark_symbol": cfg["benchmark_symbol"]}
        if self.prices.provider is None:
            base.update(series_available=False, series_reason="Live prices are off: period returns need price history")
            return base

        # ---------------------------------------------------------- daily series
        names = sorted({l.name for l in st.sheet.lots})
        codes = {}
        for l in st.sheet.lots:
            codes.setdefault(l.name, l.code)
        symbols = self.prices.symbol_map(names, codes, auto_resolve=True)
        hist = {n: _Series(self.prices.history(s, start)) if s else _Series([]) for n, s in symbols.items()}
        bench = _Series(self.prices.history(cfg["benchmark_symbol"], start))
        missing = [n for n, s in hist.items() if not s]
        cal = sorted({d for s in list(hist.values()) + [bench] for d in s.dates if start <= d < today} | {today})
        if not cal:
            base.update(series_available=False, series_reason="No price history available")
            return base
        fallback = {}  # holdings without history: valued at their latest known rate
        for l in st.sheet.lots:
            fallback[l.name] = l.market_rate or l.rate
        flows_by_day = defaultdict(float)
        for d, a, _ in flows:
            flows_by_day[d] += a
        # positions only change on event dates -> rebuild only then
        event_days = sorted({d for d, _, _ in flows} | {t.date for t in st.extras.transactions})
        units_cache: dict[dt.date, dict[str, float]] = {}

        def units_on(d):
            i = bisect.bisect_right(event_days, d) - 1
            key = event_days[i] if i >= 0 else None
            if key not in units_cache:
                held = defaultdict(float)
                if key is not None:
                    for l in P.build(st.sheet.lots, st.extras.transactions, as_of=key).open_lots:
                        held[l.name] += l.held_qty
                units_cache[key] = held
            return units_cache[key]

        live_prices = {h["name"]: h["price"] for h in ov["holdings"]}
        # prev_v starts at 0 so the first day's return is value / money paid (incl. the move from
        # purchase price to that day's close), not value / value
        series, prev_v, idx, index = [], 0.0, 1.0, []
        bench_units, prev_b, b_idx = 0.0, None, 1.0
        cum_in = 0.0
        flow_days, fi = sorted(flows_by_day), 0
        for d in cal:
            held = units_on(d)
            v = 0.0
            for n, u in held.items():
                if u <= 0:
                    continue
                px = live_prices.get(n) if d == today else None
                px = px or hist.get(n, _Series([])).on(d) or fallback.get(n) or 0.0
                v += u * px
            cf = 0.0  # money in (+) / out (-) since the previous trading day, up to and including d
            while fi < len(flow_days) and flow_days[fi] <= d:
                cf += flows_by_day[flow_days[fi]]
                fi += 1
            cum_in += cf
            # time-weighted: flows at the start of the day
            if prev_v + cf > 1e-9:
                idx *= v / (prev_v + cf)
            index.append((d, idx))
            # benchmark with the same money
            bpx = bench.on(d)
            if bpx:
                bench_units += cf / bpx
                if prev_b is not None and prev_b > 0:
                    b_idx = bpx / prev_b * b_idx
                prev_b = bpx
            series.append({"date": d.isoformat(), "value": _r(v), "invested": _r(cum_in), "flow": _r(cf),
                           "growth": _r(idx * 100, 4), "benchmark_value": _r(bench_units * bpx) if bpx else None,
                           "benchmark_growth": _r(b_idx * 100, 4) if prev_b else None})
            prev_v = v

        series_start = cal[0]
        by_date = {dt.date.fromisoformat(p["date"]): p for p in series}
        dates = sorted(by_date)

        def at(d):
            i = bisect.bisect_right(dates, d) - 1
            return by_date[dates[i]] if i >= 0 else None

        fy_s, _ = fy_bounds(today)
        periods = [("1W", today - dt.timedelta(days=7)), ("1M", add_months(today, -1)), ("3M", add_months(today, -3)),
                   ("6M", add_months(today, -6)), ("FY", fy_s - dt.timedelta(days=1)), ("1Y", add_months(today, -12)),
                   ("3Y", add_months(today, -36)), ("ALL", start)]
        rows = []
        for label, s0 in periods:
            if label != "ALL" and s0 < start:
                continue
            if label == "ALL":  # from nothing: every flow from the first day, weighted by time invested
                v0, g0, b0 = 0.0, 100.0, None
                pf = [(d, a) for d, a in flows_by_day.items() if d <= today]
            else:
                # start from the last trading day on or before s0 (s0 may be a holiday); every flow
                # after that day belongs to the period, so a sale on a holiday is never lost
                p0 = at(s0)
                if p0:
                    s0 = dt.date.fromisoformat(p0["date"])
                v0 = p0["value"] if p0 else 0.0
                g0 = p0["growth"] if p0 else 100.0
                b0 = p0["benchmark_growth"] if p0 and p0["benchmark_growth"] else None
                pf = [(d, a) for d, a in flows_by_day.items() if s0 < d <= today]
            md = modified_dietz(v0 or 0.0, value_now, pf, s0, today)
            twr = series[-1]["growth"] / g0 - 1 if g0 else None
            bt = (series[-1]["benchmark_growth"] / b0 - 1) if b0 and series[-1]["benchmark_growth"] else None
            if label == "ALL":
                bt = (series[-1]["benchmark_growth"] / 100 - 1) if series[-1]["benchmark_growth"] else None
            days = (today - s0).days
            # time-weighted figures can only cover days that have prices
            twr_days = (today - max(s0, series_start)).days

            def ann(r, n=twr_days):
                return _r(((1 + r) ** (365 / n) - 1) * 100) if r is not None and n >= 365 and r > -1 else None
            rows.append({"period": label, "from": max(s0, start).isoformat(), "days": days,
                         "start_value": _r(v0), "end_value": _r(value_now), "net_invested": _r(sum(a for _, a in pf)),
                         "gain": _r(value_now - (v0 or 0) - sum(a for _, a in pf)),
                         "md_pct": _r(md * 100) if md is not None else None,
                         "twr_pct": _r(twr * 100) if twr is not None else None,
                         "benchmark_pct": _r(bt * 100) if bt is not None else None,
                         "twr_from": max(s0, series_start).isoformat(),
                         "twr_annualised_pct": ann(twr), "benchmark_annualised_pct": ann(bt)})
        dd = max_drawdown(index)
        bench_val = series[-1]["benchmark_value"]
        base.update(series_available=True, periods=rows, max_drawdown=dd,
                    benchmark={"value_same_money": bench_val, "your_value": _r(value_now),
                               "difference": _r(value_now - bench_val) if bench_val is not None else None},
                    series=series[-400:] if len(series) <= 400 else series[:: max(1, len(series) // 400)] + [series[-1]],
                    missing_history=missing, series_start=series_start.isoformat())
        base["summary"]["twr_since_start_pct"] = next((r["twr_pct"] for r in rows if r["period"] == "ALL"), None)
        return base
