"""Performance metrics (hand-checkable cases with a fake price history)."""
import datetime as dt

import pytest

from app.config import Config
from app.container import build
from app.domain.models import NewLot, Transaction, TxnType
from app.services.performance_service import max_drawdown, modified_dietz

D = dt.date


def test_modified_dietz_counts_money_only_while_invested():
    # 100 invested at the start, another 100 halfway; ends at 230 -> gain 30 on avg capital 150
    r = modified_dietz(100, 230, [(D(2026, 1, 16), 100)], D(2026, 1, 1), D(2026, 1, 31))
    assert r == pytest.approx(30 / 150)
    # since inception (v0 = 0): a single deposit on day one is weighted fully
    assert modified_dietz(0, 110, [(D(2026, 1, 1), 100)], D(2026, 1, 1), D(2026, 1, 31)) == pytest.approx(0.10)


def test_max_drawdown():
    idx = [(D(2026, 1, i), v) for i, v in enumerate([1.0, 1.2, 0.9, 1.1, 1.3], start=1)]
    dd = max_drawdown(idx)
    assert dd["pct"] == pytest.approx(-25.0) and dd["peak"] == "2026-01-02" and dd["trough"] == "2026-01-03"


class Prov:
    """Stock X doubles over 20 days; index NIFTY rises 10%."""
    name = "fake"

    def __init__(self, start):
        self.start = start

    def _hist(self, a, b):
        return [(self.start + dt.timedelta(days=i), a + (b - a) * i / 20) for i in range(21)]

    def quotes(self, syms):
        last = {"X.NS": 200.0, "^NSEI": 110.0}
        return {s: {"price": last[s], "prev_close": None, "time": 0} for s in syms if s in last}

    def history(self, sym, start):
        return {"X.NS": self._hist(100, 200), "^NSEI": self._hist(100, 110)}.get(sym, [])

    def resolve(self, q):
        return [{"symbol": "X.NS", "name": "X", "type": "EQUITY"}] if q.upper() == "X" else []


def test_period_returns_twr_and_same_money_benchmark(tmp_path, sheet_bytes):
    """Sheet: SBI ETF GOLD (60 units, no price history -> flat at its sheet rate 101.68) plus
    X: 10 @ 100 on day 0 and 10 @ 150 on day 10. X goes 100 -> 200 linearly over 20 days
    (+5/day); the index goes 100 -> 110."""
    start = D.today() - dt.timedelta(days=20)
    c = build(Config(tmp_path, None, b"k", 1, 5, True, None, False, 0), price_provider=Prov(start))
    from app.dao.xlsx.invt_sheet import InvtSheetDAO
    data, _ = InvtSheetDAO().append(sheet_bytes, [NewLot("X", "FMCG", start, 10, 100, 0, code="X"),
                                                   NewLot("X", "FMCG", start + dt.timedelta(days=10), 10, 150, 0, code="X")])
    c.versions.upload(data, "Invt.xlsx")
    perf = c.performance.compute()
    assert perf["available"] and perf["series_available"]
    sbi = 60 * 101.68
    rows = {r["period"]: r for r in perf["periods"]}
    # 1 week: no money moved; value 6100.8 + 20*165 (day 13) -> 6100.8 + 20*200 (today)
    v13, v20 = sbi + 20 * 165, sbi + 20 * 200
    assert rows["1W"]["md_pct"] == pytest.approx((v20 / v13 - 1) * 100, abs=0.01)
    assert rows["1W"]["twr_pct"] == pytest.approx((v20 / v13 - 1) * 100, abs=0.01)
    assert rows["1W"]["benchmark_pct"] == pytest.approx((110 / 106.5 - 1) * 100, abs=0.01)
    # same-money benchmark: all money up to day 0 bought the index at 100, day-10 money at 105
    units = (6181.24 + 1000) / 100 + 1500 / 105
    assert perf["benchmark"]["value_same_money"] == pytest.approx(units * 110, abs=0.05)
    # TWR since the start: the 1500 added on day 10 does not distort it
    d0, d9, d10 = sbi + 10 * 100, sbi + 10 * 145, sbi + 20 * 150
    paid0 = 6181.24 + 1000  # everything bought up to day 0 (SBI earlier, X on day 0), at cost
    twr = (d0 / paid0) * (d9 / d0) * (d10 / (d9 + 1500)) * (v20 / d10)
    # (daily chaining telescopes within each flow-free stretch)
    assert perf["summary"]["twr_since_start_pct"] == pytest.approx((twr - 1) * 100, abs=0.01)
    x = next(h for h in perf["contributors"] if h["name"] == "X")
    assert x["unrealised"] == pytest.approx(20 * 200 - 1000 - 1500)
    assert rows["ALL"]["twr_annualised_pct"] is None  # < 1 year: never annualised


def test_sell_counts_in_total_pnl(tmp_path, sheet_bytes):
    c = build(Config(tmp_path, None, b"k", 1, 5, False, None, False, 0), price_provider=None)
    c.versions.upload(sheet_bytes, "Invt.xlsx")
    c.transactions.add(Transaction("", D(2026, 10, 20), TxnType.SELL, "SBI ETF GOLD", 20, 120.0, charges=0.5))
    p = c.performance.compute()
    s = p["summary"]
    assert s["realised"] == pytest.approx(20 * 120 - 0.5 - 6181.24 / 3, abs=0.01)
    assert s["total_pnl"] == pytest.approx(s["realised"] + s["unrealised"], abs=0.01)
    assert s["total_withdrawn"] == pytest.approx(2399.5)
    assert s["md_since_start_pct"] is not None and p["series_available"] is False  # no live prices -> no series


def test_single_purchase_twr_equals_money_weighted(tmp_path, sheet_bytes):
    """With one purchase and nothing else, both returns must agree (cost -> value today)."""
    start = D.today() - dt.timedelta(days=20)
    c = build(Config(tmp_path, None, b"k", 1, 5, True, None, False, 0), price_provider=Prov(start))
    c.versions.upload(sheet_bytes, "Invt.xlsx")  # only SBI ETF GOLD, flat at 101.68
    s = c.performance.compute()["summary"]
    assert s["twr_since_start_pct"] == pytest.approx(s["md_since_start_pct"], abs=0.01)
    assert s["md_since_start_pct"] == pytest.approx((60 * 101.68 / 6181.24 - 1) * 100, abs=0.01)


def test_flow_on_a_holiday_period_start_is_counted(tmp_path, sheet_bytes):
    """Period starts on a day with no prices (holiday) and money moved that day: the period
    must start from the previous trading day's value and include that flow."""
    start = D.today() - dt.timedelta(days=20)

    class Holiday(Prov):
        def _hist(self, a, b):  # no price on day 13 (= today - 7, the 1W start)
            return [x for x in super()._hist(a, b) if x[0] != start + dt.timedelta(days=13)]

    c = build(Config(tmp_path, None, b"k", 1, 5, True, None, False, 0), price_provider=Holiday(start))
    c.versions.upload(sheet_bytes, "Invt.xlsx")
    c.transactions.add(Transaction("", start + dt.timedelta(days=13), TxnType.SELL, "SBI ETF GOLD", 30, 101.68))
    w = {r["period"]: r for r in c.performance.compute()["periods"]}["1W"]
    assert w["from"] == (start + dt.timedelta(days=12)).isoformat()
    assert w["net_invested"] == pytest.approx(-30 * 101.68, abs=0.01)
    assert w["md_pct"] == pytest.approx(0.0, abs=0.01)  # flat price: selling at market is not a loss
