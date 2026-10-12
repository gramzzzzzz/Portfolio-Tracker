"""Price writes into the sheet: DAO edit, in-place HEAD replacement, mirror, locking."""
import datetime as dt
import threading

import pytest

from app.config import Config
from app.container import build
from app.dao.xlsx.invt_sheet import InvtSheetDAO
from app.dao.xlsx.workbook import Workbook
from app.domain.models import NewLot

D = dt.date(2026, 10, 1)


class FakeProvider:
    name = "fake"

    def __init__(self, prices):
        self.prices = prices
        self.calls = 0

    def quotes(self, symbols):
        self.calls += 1
        t = int(dt.datetime(2026, 10, 9, 15, 30, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30))).timestamp())
        return {s: {"price": self.prices[s], "prev_close": None, "time": t} for s in symbols if s in self.prices}

    def history(self, symbol, start):
        return []

    def resolve(self, query):
        return [{"symbol": query.upper().replace(" ", "") + ".NS", "name": query, "type": "EQUITY"}]


def make(tmp_path, provider):
    cfg = Config(data_dir=tmp_path, seed_sheet=None, secret_key=b"k", session_hours=1, max_upload_mb=5,
                 live_prices=True, static_dir=None, cookie_secure=False, price_sync_minutes=0)
    return build(cfg, price_provider=provider)


def test_update_prices_only_touches_market_rate_and_date(sheet_bytes):
    dao = InvtSheetDAO()
    out, res = dao.update_prices(sheet_bytes, {"SBI ETF GOLD": 128.456, "UNKNOWN": 5}, dt.date(2026, 10, 9))
    assert res.changed and res.updated_rows == [31] and res.valuation_cell == "L3"
    assert all(c["passed"] for c in res.checks)
    before, after = Workbook(sheet_bytes).sheet("Shares").cells(), Workbook(out).sheet("Shares").cells()
    changed = {k for k in before if (before[k].value, before[k].formula) != (after[k].value, after[k].formula)
               and before[k].formula is None}
    assert changed == {(31, 12), (3, 12)}  # L31 and L3 only (formula cached values may change)
    assert all(before[k].formula == after[k].formula for k in before)
    snap = dao.read(out)
    assert snap.lots[0].market_rate == 128.46 and snap.valuation_date == dt.date(2026, 10, 9)
    again, res2 = dao.update_prices(out, {"SBI ETF GOLD": 128.46}, dt.date(2026, 10, 9))
    assert again is None and not res2.changed  # nothing to do -> no write


def test_formula_market_rate_is_never_overwritten(sheet_bytes):
    wb = Workbook(sheet_bytes)
    sh = wb.sheet("Shares")
    sh.set_cell(31, 12, formula="F31*1")
    out, res = InvtSheetDAO().update_prices(wb.to_bytes(), {"SBI ETF GOLD": 140}, None)
    assert res.skipped_formula_rows == [31] and out is None


def test_sync_rewrites_head_in_place_and_mirrors(tmp_path, sheet_bytes):
    c = make(tmp_path, FakeProvider({"SBIETFGOLD.NS": 130.0}))
    v1 = c.versions.upload(sheet_bytes, "Invt.xlsx")
    old_sha = v1["sha256"]
    st = c.price_sync.run_once("test")
    assert st["result"] == "written" and st["rows_updated"] == 1 and st["valuation_date"] == "2026-10-09"
    vs = c.versions.list()
    assert len(vs) == 1 and vs[0]["sha256"] != old_sha and vs[0]["prices_at"]  # no new version
    assert not c.versions.blobs.path(old_sha).exists()  # superseded blob cleaned up
    mirror = tmp_path / "current" / "Invt.xlsx"
    assert mirror.read_bytes() == c.versions.head_bytes()
    assert c.versions.snapshot().lots[0].market_rate == 130.0
    assert c.price_sync.run_once("test")["result"] == "unchanged"


def test_restore_keeps_blob_shared_with_other_version(tmp_path, sheet_bytes):
    c = make(tmp_path, FakeProvider({"SBIETFGOLD.NS": 131.0}))
    v1 = c.versions.upload(sheet_bytes, "Invt.xlsx")
    c.versions.restore(v1["id"])  # v2 has the same content as v1
    c.price_sync.run_once("test")  # rewrites v2 only
    assert c.versions.blobs.path(v1["sha256"]).exists()  # still referenced by v1
    assert c.versions.bytes_of(v1["id"]) == sheet_bytes


def test_lock_is_reentrant_and_exclusive(tmp_path):
    from app.services.sheet_lock import SheetLock

    lock = SheetLock(tmp_path / ".lock", timeout=0.3)
    order = []
    with lock:
        with lock:  # nested acquisition by the same thread
            order.append("outer")

        def other():
            try:
                with lock:
                    order.append("other")
            except Exception as e:
                order.append(type(e).__name__)

        t = threading.Thread(target=other)
        t.start()
        t.join()
    assert order == ["outer", "LockTimeout"]
    with lock:  # released properly
        pass


def test_price_syncs_racing_imports_lose_nothing(tmp_path, sheet_bytes):
    """Hammer price syncs and imports concurrently: every imported row must survive and the
    sheet must always be internally consistent."""
    prov = FakeProvider({"SBIETFGOLD.NS": 100.0})
    c = make(tmp_path, prov)
    c.versions.upload(sheet_bytes, "Invt.xlsx")
    dao, stop, errors = c.versions.dao, threading.Event(), []

    def syncer():
        i = 0
        while not stop.is_set():
            prov.prices["SBIETFGOLD.NS"] = 100.0 + (i % 7)
            c.repo.cache_put("quote:SBIETFGOLD.NS", {})  # defeat the 5-min quote cache
            with c.repo.tx() as conn:
                conn.execute("DELETE FROM cache WHERE key LIKE 'quote:%'")
            r = c.price_sync.run_once("race")
            if r.get("result") == "error":
                errors.append(r.get("detail"))
            i += 1

    t = threading.Thread(target=syncer)
    t.start()
    try:
        for i in range(6):
            with c.versions.write_lock:
                new, res = dao.append(c.versions.head_bytes(), [NewLot(f"STOCK {i}", "FMCG", D, 1, 100 + i, 0.1)])
                c.versions.commit(new, message=f"import {i}", source="import")
    finally:
        stop.set()
        t.join()
    assert not errors
    snap = c.versions.snapshot()
    assert {f"STOCK {i}" for i in range(6)} <= {l.name for l in snap.lots}
    assert len(c.versions.list()) == 7
    assert snap.totals["summary_total"] == pytest.approx(snap.totals["grand_total_cost"])
    assert (tmp_path / "current" / "Invt.xlsx").read_bytes() == c.versions.head_bytes()
