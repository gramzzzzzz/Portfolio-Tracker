"""Transactions sheet (sells / bonus / splits), positions engine and the guarantee that the
original tracking sheet is never affected."""
import datetime as dt
import io
import zipfile

import openpyxl
import pytest

from app.config import Config
from app.container import build
from app.dao.xlsx import app_sheets as A
from app.dao.xlsx.extra_sheets import ExtraSheetsDAO
from app.dao.xlsx.invt_sheet import InvtSheetDAO
from app.dao.xlsx.workbook import Workbook
from app.domain.models import Lot, NewLot, Transaction, TxnType
from app.services import positions as P

D = dt.date
PROTECTED = ("xl/worksheets/sheet1.xml", "xl/sharedStrings.xml", "xl/calcChain.xml")


def part(b, name):
    return zipfile.ZipFile(io.BytesIO(b)).read(name)


def lot(row, name, date, qty, rate, ch=0.0):
    return Lot(row, name, None, date, None, qty, rate, ch, qty * rate + ch, qty, rate, None, 0, "X")


@pytest.fixture
def app(tmp_path, sheet_bytes):
    c = build(Config(tmp_path, None, b"k", 1, 5, False, None, False, 0), price_provider=None)
    c.versions.upload(sheet_bytes, "Invt.xlsx")
    return c


# ------------------------------------------------------------------- app sheets
def test_app_sheets_never_touch_the_original_sheet(sheet_bytes):
    dao = ExtraSheetsDAO()
    out = dao.write(sheet_bytes, transactions=[Transaction("T-1", D(2026, 10, 1), TxnType.SELL, "SBI ETF GOLD", 5, 120.0)])
    out = dao.write(out, transactions=[Transaction("T-1", D(2026, 10, 1), TxnType.SELL, "SBI ETF GOLD", 6, 121.0)])
    for p in PROTECTED:
        assert part(sheet_bytes, p) == part(out, p)
    zi, zo = zipfile.ZipFile(io.BytesIO(sheet_bytes)), zipfile.ZipFile(io.BytesIO(out))
    changed = {n for n in zi.namelist() if zi.read(n) != zo.read(n)}
    assert changed <= {"[Content_Types].xml", "xl/workbook.xml", "xl/_rels/workbook.xml.rels", "xl/styles.xml"}
    assert InvtSheetDAO().read(out).totals == InvtSheetDAO().read(sheet_bytes).totals
    wb = openpyxl.load_workbook(io.BytesIO(out))
    assert wb.sheetnames == ["Shares", "Transactions"]  # appended after the original sheet
    assert dao.read(out).transactions[0].qty == 6  # rewritten, not duplicated


def test_styles_only_appended_and_idempotent(sheet_bytes):
    dao = ExtraSheetsDAO()
    o1 = dao.write(sheet_bytes, transactions=[])
    o2 = dao.write(o1, transactions=[])
    assert part(o1, "xl/styles.xml") == part(o2, "xl/styles.xml")  # no growth on rewrite


def test_refuses_to_overwrite_protected_sheet(sheet_bytes):
    spec = A.TableSpec("Shares", (A.Column("a", "A"),), "x")
    with pytest.raises(A.AppSheetError):
        A.write_tables(sheet_bytes, [(spec, [])], protect="Shares")


def test_sheet_edited_in_excel_still_reads(sheet_bytes):
    """openpyxl re-save converts inline strings to shared strings, like Excel does."""
    dao = ExtraSheetsDAO()
    out = dao.write(sheet_bytes, transactions=[Transaction("T-1", D(2026, 10, 1), TxnType.BONUS, "SBI ETF GOLD", 6)])
    wb = openpyxl.load_workbook(io.BytesIO(out))
    wb["Transactions"].append(["T-2", dt.datetime(2026, 10, 2), "sell", "SBI ETF GOLD", 3, 130])
    buf = io.BytesIO()
    wb.save(buf)
    ex = dao.read(buf.getvalue())
    assert [(t.id, t.type, t.qty) for t in ex.transactions] == [("T-1", TxnType.BONUS, 6), ("T-2", TxnType.SELL, 3)]


def test_bad_rows_are_reported_not_fatal(sheet_bytes):
    dao = ExtraSheetsDAO()
    out = dao.write(sheet_bytes, transactions=[])
    wb = openpyxl.load_workbook(io.BytesIO(out))
    wb["Transactions"].append(["X", dt.datetime(2026, 1, 1), "DIVIDEND", "A", 1])
    wb["Transactions"].append(["Y", dt.datetime(2026, 1, 1), "SELL", "A", 1])  # no price
    buf = io.BytesIO()
    wb.save(buf)
    ex = dao.read(buf.getvalue())
    assert ex.transactions == [] and len(ex.warnings) == 2


# --------------------------------------------------------------- positions engine
def test_fifo_sell_bonus_split():
    lots = [lot(5, "ITC", D(2024, 1, 1), 10, 100, 1), lot(6, "ITC", D(2025, 1, 1), 10, 200, 2)]
    tx = [Transaction("B", D(2025, 6, 1), TxnType.BONUS, "ITC", 5),
          Transaction("S", D(2026, 1, 1), TxnType.SPLIT, "itc", ratio=2),
          Transaction("X", D(2026, 3, 1), TxnType.SELL, "ITC", 25, price=150)]
    p = P.build(lots, tx)
    sale = p.sales[0]
    # after split: row5 20 units, row6 20 units, bonus 10 units; sell 25 -> row5 (20) + row6 (5)
    assert [(l.lot_row, l.units) for l in sale.legs] == [(5, 20), (6, 5)]
    assert sale.legs[0].sheet_cost == pytest.approx(1001) and sale.legs[1].sheet_cost == pytest.approx(500.5)
    held = {(l.row, l.origin): (l.held_qty, round(l.cost, 2)) for l in p.open_lots}
    assert held == {(6, "purchase"): (15, 1501.5), (P.BONUS_ROW_BASE + 1, "bonus"): (10, 0.0)}
    assert lots[0].qty == 10 and lots[0].cost == 1001  # inputs never mutated


def test_bonus_holding_period_starts_on_allotment():
    p = P.build([lot(5, "A", D(2020, 1, 1), 10, 100)], [Transaction("B", D(2026, 9, 1), TxnType.BONUS, "A", 10)])
    bonus = next(l for l in p.open_lots if l.origin == "bonus")
    assert bonus.date == D(2026, 9, 1) and bonus.cost == 0


def test_oversell_and_unknown_are_issues():
    p = P.build([lot(5, "A", D(2024, 1, 1), 10, 100)],
                [Transaction("X", D(2025, 1, 1), TxnType.SELL, "A", 15, price=1),
                 Transaction("Y", D(2025, 1, 1), TxnType.SELL, "NOPE", 1, price=1)])
    codes = {i["code"] for i in p.issues}
    assert codes == {"sell_exceeds_holding", "txn_unknown_security"} and p.sales[0].unmatched_units == 5


def test_holdings_as_of_date():
    lots = [lot(5, "A", D(2024, 1, 1), 10, 100), lot(6, "A", D(2025, 6, 1), 5, 100)]
    tx = [Transaction("X", D(2025, 1, 1), TxnType.SELL, "A", 4, price=110)]
    assert P.build(lots, tx, as_of=D(2024, 12, 31)).held("A") == 10
    assert P.build(lots, tx, as_of=D(2025, 1, 1)).held("A") == 6
    assert P.build(lots, tx).held("A") == 11


# ------------------------------------------------------------ through the app
def test_recorded_sell_changes_holdings_and_realised_tax_not_the_sheet(app, sheet_bytes):
    before = app.versions.head_bytes()
    app.transactions.add(Transaction("", D(2026, 10, 20), TxnType.SELL, "SBI ETF GOLD", 20, 120.0, charges=0.5))
    head = app.versions.head_bytes()
    for p in PROTECTED:
        assert part(before, p) == part(head, p)
    st = app.portfolio.state()
    assert st.sheet.lots[0].held_qty == 60 and st.positions.held("SBI ETF GOLD") == 40
    sales = app.tax.recorded_sales(D(2026, 10, 26))
    # 20 x 120 - 0.5 - (6181.24 x 20/60) = 1739.09... gold ETF held < 12 months -> slab
    assert sales[0]["gain"] == pytest.approx(2400 - 0.5 - 6181.24 / 3, abs=0.01)
    assert sales[0]["gains"]["st_slab"] == pytest.approx(sales[0]["gain"])
    g, _ = app.tax.realised(D(2026, 10, 26))
    assert g.st_slab == pytest.approx(sales[0]["gain"])
    ov = app.analytics.overview(app.portfolio.snapshot())
    assert ov["holdings"][0]["qty"] == 40 and ov["kpis"]["invested"] == pytest.approx(6181.24 * 2 / 3, abs=0.01)
    assert app.versions.list()[0]["source"] == "transactions"


def test_cannot_sell_more_than_held_on_that_date(app):
    with pytest.raises(ValueError, match="held 0"):
        app.transactions.add(Transaction("", D(2025, 1, 1), TxnType.SELL, "SBI ETF GOLD", 1, 100.0))
    with pytest.raises(ValueError, match="held 60"):
        app.transactions.add(Transaction("", D(2026, 1, 1), TxnType.SELL, "SBI ETF GOLD", 61, 100.0))
    with pytest.raises(ValueError, match="no purchase rows"):
        app.transactions.add(Transaction("", D(2026, 1, 1), TxnType.BONUS, "UNKNOWN", 1))


def test_deleting_bonus_that_later_sells_rely_on_is_blocked(app):
    out = app.transactions.add(Transaction("", D(2026, 1, 1), TxnType.BONUS, "SBI ETF GOLD", 10))
    bonus_id = out["transactions"][0]["id"]
    app.transactions.add(Transaction("", D(2026, 2, 1), TxnType.SELL, "SBI ETF GOLD", 70, 110.0))
    with pytest.raises(ValueError, match="Cannot delete"):
        app.transactions.delete(bonus_id)


def test_imports_still_insert_rows_with_app_sheets_present(app):
    app.transactions.add(Transaction("", D(2026, 10, 1), TxnType.BONUS, "SBI ETF GOLD", 2))
    dao = InvtSheetDAO()
    new, res = dao.append(app.versions.head_bytes(), [NewLot("ITC", "FMCG", D(2026, 10, 2), 10, 260, 3.0)])
    assert all(c["passed"] for c in res.checks)
    assert ExtraSheetsDAO().read(new).transactions[0].qty == 2  # app sheet survives row insertion
    wb = Workbook(new)
    assert "Transactions" in wb.sheet_names()


def test_history_holdings_on_dates(app):
    app.transactions.add(Transaction("", D(2026, 10, 20), TxnType.SELL, "SBI ETF GOLD", 10, 120.0))
    h = app.history.holdings_on(D(2026, 10, 19))
    assert h["holdings"][0]["units"] == 60
    h = app.history.holdings_on(D(2026, 10, 21))
    assert h["holdings"][0]["units"] == 50
    assert app.history.holdings_on(D(2020, 1, 1))["holdings"] == []
    ev = app.history.events()
    assert [e["type"] for e in ev["events"]] == ["SELL", "BUY"]
    assert {c["id"]: c["supported"] for c in ev["asset_classes"]}["fd"] is False


def test_every_sheet_change_is_versioned_and_diffable(app):
    n0 = len(app.versions.list())
    app.transactions.add(Transaction("", D(2026, 10, 20), TxnType.SELL, "SBI ETF GOLD", 10, 120.0))
    tid = app.transactions.list()["transactions"][0]["id"]
    app.transactions.update(tid, Transaction(tid, D(2026, 10, 20), TxnType.SELL, "SBI ETF GOLD", 12, 121.0))
    app.transactions.delete(tid)
    from app.domain.models import GoldItem
    app.gold.add(GoldItem("", D(2023, 5, 1), "Coin", "coin", "24K", 10, 60000))
    vs = app.versions.list()
    assert len(vs) == n0 + 4 and [v["source"] for v in vs[:4]] == ["gold", "transactions", "transactions", "transactions"]
    d_add, d_edit, d_del, d_gold = (app.versions.diff(v["id"]) for v in reversed(vs[:4]))
    assert d_add["transactions"]["added"][0]["qty"] == 10
    assert d_edit["transactions"]["changed"][0]["after"]["qty"] == 12
    assert d_del["transactions"]["removed"][0]["id"] == tid
    assert d_gold["gold"]["added"][0]["description"] == "Coin" and not d_gold["added_lots"]
    # restoring the version before the sale brings the Transactions sheet back with it
    app.versions.restore(vs[3]["id"])
    assert [t.qty for t in app.portfolio.state().extras.transactions] == [10]
    assert app.portfolio.state().extras.gold == []


def test_gold_photos_survive_a_failed_delete(app, monkeypatch):
    from app.domain.models import GoldItem
    gid = app.gold.add(GoldItem("", D(2023, 5, 1), "Coin", "coin", "24K", 10, 60000))["items"][0]["id"]
    app.gold.add_photo(gid, b"\x89PNG\r\n\x1a\n" + b"0" * 20, "a.png")

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(app.gold, "_commit", boom)
    with pytest.raises(RuntimeError):
        app.gold.delete(gid)
    assert len(app.repo.gold_photos(gid)) == 1
