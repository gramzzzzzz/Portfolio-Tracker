"""Physical gold: price, valuation, tax, Gold sheet, photos (hand-calculated cases)."""
import datetime as dt
import io
import zipfile

import pytest

from app.config import Config
from app.container import build
from app.domain.models import GoldItem

D = dt.date
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class FakeProv:
    name = "fake"

    def __init__(self, gold=4000.0, fx=95.0):
        self.p = {"GC=F": gold, "INR=X": fx}

    def quotes(self, syms):
        return {s: {"price": self.p[s], "prev_close": None, "time": 1792900000} for s in syms if s in self.p}

    def history(self, s, d):
        return []

    def resolve(self, q):
        return []


@pytest.fixture
def app(tmp_path, sheet_bytes):
    c = build(Config(tmp_path, None, b"k", 1, 5, True, None, False, 0), price_provider=FakeProv())
    c.versions.upload(sheet_bytes, "Invt.xlsx")
    return c


def per_g_24k(premium=15.0):
    return 4000 * 95 / 31.1034768 * (1 + premium / 100)


def test_price_from_international_spot_plus_premium(app):
    p = app.gold.price()
    assert p["source"] == "live" and p["per_g_24k"] == pytest.approx(per_g_24k(), abs=0.01)
    assert p["per_g"]["22K"] == pytest.approx(per_g_24k() * 0.916 / 0.999, abs=0.01)
    intl = 4000 * 95 / 31.1034768
    b = p["breakdown"]["24K"]
    assert b["international"] == pytest.approx(intl, abs=0.01) and b["premium"] == pytest.approx(intl * 0.15, abs=0.01)
    assert b["international"] + b["premium"] == pytest.approx(b["total"], abs=0.02)
    assert p["breakdown"]["22K"]["international"] == pytest.approx(intl * 0.916 / 0.999, abs=0.01)
    app.settings.update({"gold_price_override_24k": 15000})
    assert app.gold.price()["per_g_24k"] == 15000 and app.gold.price()["source"] == "manual"
    app.settings.update({"gold_domestic_premium_pct": 0, "gold_price_override_24k": None})
    assert app.gold.price()["per_g_24k"] == pytest.approx(per_g_24k(0), abs=0.01)


def test_coin_long_term_hand_calculation(app):
    ov = app.gold.add(GoldItem("", D(2023, 5, 1), "Coin", "coin", "24K", 10, 60000))
    r = ov["items"][0]
    market = per_g_24k() * 10  # 24K coin: fineness 0.999 / 0.999
    sale = market * 0.99  # 1% buy-back deduction for coins
    gain = sale - 60000
    assert r["term"] == "LT" and r["market_value"] == pytest.approx(market, abs=0.01)
    assert r["sale_value"] == pytest.approx(sale, abs=0.01)
    assert r["tax_if_sold"] == pytest.approx(gain * 0.125 * 1.04, abs=0.02)  # 12.5% + 4% cess, no exemption
    assert r["net_if_sold"] == pytest.approx(sale - gain * 0.125 * 1.04, abs=0.02)


def test_jewellery_short_term_taxed_at_slab_and_wait_hint(app):
    today = dt.date.today()
    ov = app.gold.add(GoldItem("", today.replace(year=today.year - 1), "Chain", "jewellery", "22K", 20, 200000, 18000, 6000))
    r = ov["items"][0]
    sale = per_g_24k() * 0.916 / 0.999 * 20 * 0.95  # 5% deduction for jewellery
    gain = sale - 200000
    assert r["term"] == "ST" and r["days_to_lt"] > 300  # 24-month rule
    assert r["tax_if_sold"] == pytest.approx(gain * 0.30 * 1.04, abs=0.02)
    assert r["if_wait"]["saved"] == pytest.approx(gain * (0.30 - 0.125) * 1.04, abs=0.05)


def test_holding_period_setting_is_respected(app):
    today = dt.date.today()
    app.gold.add(GoldItem("", today.replace(year=today.year - 2) - dt.timedelta(days=30), "Bar", "bar", "24K", 5, 30000))
    assert app.gold.overview()["items"][0]["term"] == "LT"
    app.settings.update({"gold_lt_months": 36})
    assert app.gold.overview()["items"][0]["term"] == "ST"


def test_sold_gold_feeds_realised_gains(app):
    ov = app.gold.add(GoldItem("", D(2023, 5, 1), "Coin", "coin", "24K", 10, 60000))
    gid = ov["items"][0]["id"]
    sold = D.today() - dt.timedelta(days=1)
    app.gold.update(gid, GoldItem(gid, D(2023, 5, 1), "Coin", "coin", "24K", 10, 60000, sold_date=sold, sold_amount=130000))
    g = app.tax.realised(D.today())[0]
    assert g.lt_other == pytest.approx(70000)
    assert app.tax.summary(app.portfolio.snapshot())["realised"]["gold"][0]["gain"] == 70000


def test_gold_sheet_written_and_original_sheet_untouched(app, sheet_bytes):
    app.gold.add(GoldItem("", D(2023, 5, 1), "Coin", "coin", "24K", 10, 60000))
    head = app.versions.head_bytes()
    z0, z1 = zipfile.ZipFile(io.BytesIO(sheet_bytes)), zipfile.ZipFile(io.BytesIO(head))
    for p in ("xl/worksheets/sheet1.xml", "xl/sharedStrings.xml", "xl/calcChain.xml"):
        assert z0.read(p) == z1.read(p)
    g = app.portfolio.state().extras.gold[0]
    assert g.rate_per_g == pytest.approx(per_g_24k(), abs=0.01) and g.current_value == pytest.approx(per_g_24k() * 10, abs=0.1)
    assert app.versions.list()[0]["source"] == "gold"


def test_price_sync_refreshes_gold_values(app):
    app.gold.add(GoldItem("", D(2023, 5, 1), "Coin", "coin", "24K", 10, 60000))
    versions = len(app.versions.list())
    app.prices.provider.p["GC=F"] = 4100.0
    with app.repo.tx() as conn:
        conn.execute("DELETE FROM cache")
    st = app.price_sync.run_once("test")
    assert st["gold_written"] and len(app.versions.list()) == versions  # in place, no new version
    assert app.portfolio.state().extras.gold[0].rate_per_g == pytest.approx(4100 * 95 / 31.1034768 * 1.15, abs=0.01)


def test_validation(app):
    bad = [
        GoldItem("", D(2023, 1, 1), "x", "ring", "22K", 1, 100),
        GoldItem("", D(2023, 1, 1), "x", "coin", "22K", 0, 100),
        GoldItem("", D.today() + dt.timedelta(days=1), "x", "coin", "22K", 1, 100),
        GoldItem("", D(2023, 1, 1), "x", "coin", "22K", 1, 100, making_charges=90, gst_paid=20),
        GoldItem("", D(2023, 1, 1), "x", "coin", "22K", 1, 100, sold_date=D(2022, 1, 1), sold_amount=1),
    ]
    for g in bad:
        with pytest.raises(ValueError):
            app.gold.add(g)


def test_photos(app):
    gid = app.gold.add(GoldItem("", D(2023, 5, 1), "Coin", "coin", "24K", 10, 60000))["items"][0]["id"]
    pid = app.gold.add_photo(gid, PNG, "front.png")["id"]
    data, mime, name = app.gold.photo(pid)
    assert data == PNG and mime == "image/png" and name == "front.png"
    assert app.gold.overview()["items"][0]["photos"] == [{"id": pid, "filename": "front.png"}]
    with pytest.raises(ValueError):
        app.gold.add_photo(gid, b"<svg onload=alert(1)>", "x.svg")  # not an allowed image type
    app.gold.delete(gid)  # deleting the item removes its photos
    with pytest.raises(KeyError):
        app.gold.photo(pid)
