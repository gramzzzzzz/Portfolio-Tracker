"""Physical gold: live price, valuation, sale value, tax - all assumptions editable in Settings.

Price: Yahoo ``GC=F`` (USD per troy ounce) x ``INR=X`` / 31.1035 g = international 24K price
per gram, plus a domestic premium (import duty 15% since 13 May 2026 + local premium). The
owner can instead type their jeweller's 24K rate. Lower purities scale by fineness.

Selling physical gold (by an individual):
  * the buyer pays the market rate minus a buy-back deduction (higher for jewellery, which
    is melted and assayed); making charges and GST are not recovered;
  * capital gains: long term after 24 months -> 12.5 % without indexation (no exemption);
    up to 24 months -> added to income, taxed at the slab rate; plus surcharge and cess;
  * cost of acquisition = everything paid (gold + making charges + GST), as per the invoice.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import re

from app.dao.repo.blobs import BlobStore
from app.dao.repo.sqlite_repo import Repository
from app.dao.xlsx.extra_sheets import ExtraSheetsDAO, new_id
from app.domain.models import GoldItem
from app.services import tax_engine as te
from app.services.finance import add_months, fy_bounds, fy_label
from app.services.portfolio_service import PortfolioService
from app.services.prices_service import PriceService
from app.services.settings_service import SettingsService
from app.services.version_service import VersionService

log = logging.getLogger(__name__)
TROY_OZ_G = 31.1034768
FORMS = ("coin", "bar", "jewellery")
MAX_PHOTO = 8 * 1024 * 1024
IMAGE_MAGIC = {b"\xff\xd8\xff": "image/jpeg", b"\x89PNG\r\n\x1a\n": "image/png", b"RIFF": "image/webp",
               b"GIF8": "image/gif"}


def _r(x, n=2):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(x, n) + 0.0


def sniff_image(data: bytes) -> str | None:
    for magic, mime in IMAGE_MAGIC.items():
        if data.startswith(magic):
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue
            return mime
    if len(data) > 12 and data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc"):
        return "image/heic"
    return None


class GoldService:
    def __init__(self, repo: Repository, versions: VersionService, extras: ExtraSheetsDAO,
                 portfolio: PortfolioService, prices: PriceService, settings: SettingsService, photos: BlobStore):
        self.repo = repo
        self.versions = versions
        self.extras = extras
        self.portfolio = portfolio
        self.prices = prices
        self.settings = settings
        self.photos = photos
        self.tax = None  # TaxService (attached by the container)

    # ------------------------------------------------------------------ price
    def price(self) -> dict:
        c = self.settings.all()
        override = c["gold_price_override_24k"]
        out = {"source": None, "per_g_24k": None, "intl_per_g_24k": None, "usd_oz": None, "usd_inr": None,
               "premium_pct": c["gold_domestic_premium_pct"], "time": None}
        if self.prices.provider is not None:
            q = self.prices.quotes(["GC=F", "INR=X"])
            g, fx = q.get("GC=F"), q.get("INR=X")
            if g and fx and g.get("price") and fx.get("price"):
                intl = g["price"] * fx["price"] / TROY_OZ_G
                # unrounded: values are rate x weight, rounding happens only for display
                out.update(usd_oz=_r(g["price"]), usd_inr=_r(fx["price"], 4), intl_per_g_24k=_r(intl),
                           per_g_24k=intl * (1 + c["gold_domestic_premium_pct"] / 100), source="live",
                           time=g.get("time"), change_pct=_r(g.get("change_pct")))
        if override:
            out.update(per_g_24k=float(override), source="manual")
        if out["per_g_24k"] is None:
            # offline and no override: last value written to the Gold sheet
            try:
                items = self.portfolio.state().extras.gold
            except Exception:
                items = []
            last = [(i.value_date, i.rate_per_g, self.fineness(i.purity)) for i in items if i.rate_per_g and i.value_date]
            if last:
                d, rate, fine = max(last, key=lambda x: x[0])
                out.update(per_g_24k=rate / fine * self.fineness("24K"), source="last_sync", time=None)
        p24 = out["per_g_24k"]
        f24 = self.fineness("24K")
        out["per_g"] = {k: _r(p24 * self.fineness(k) / f24) for k in c["gold_purity"]} if p24 else {}
        # breakdown: international price -> Indian premium (duty etc.) -> price used
        intl = out.get("intl_per_g_24k")
        out["breakdown"] = {
            k: {"international": _r(intl * self.fineness(k) / f24) if intl else None,
                "premium": _r((p24 - intl) * self.fineness(k) / f24) if intl and p24 else None,
                "total": _r(p24 * self.fineness(k) / f24) if p24 else None}
            for k in c["gold_purity"]}
        out["premium_per_g_24k"] = _r(p24 - intl) if intl and p24 else None
        out["_per_g_24k_exact"] = p24
        out["per_g_24k"] = _r(p24) if p24 else None
        return out

    def fineness(self, purity: str) -> float:
        table = self.settings.get("gold_purity")
        p = (purity or "").strip().upper()
        if p in table:
            return float(table[p])
        m = re.fullmatch(r"(\d{1,2})K", p)
        if m:
            return int(m.group(1)) / 24
        try:
            f = float(p)
            return f / 1000 if f > 1 else f
        except ValueError:
            return float(table.get("22K", 0.916))

    def rate_per_g(self, item: GoldItem, price: dict) -> float | None:
        p24 = price.get("_per_g_24k_exact") or price.get("per_g_24k")
        if not p24:
            return None
        return p24 * self.fineness(item.purity) / self.fineness("24K")

    # ------------------------------------------------------------ valuation
    def rules(self) -> te.TaxRules:
        c = self.settings.all()
        return te.TaxRules(stcg_equity_pct=c["stcg_rate_pct"], ltcg_pct=c["gold_ltcg_rate_pct"], ltcg_exemption=0.0,
                           slab_pct=c["slab_rate_pct"], surcharge_pct=c["surcharge_pct"], cess_pct=c["cess_pct"])

    def lt_date(self, item: GoldItem) -> dt.date:
        return add_months(item.date, int(self.settings.get("gold_lt_months")))

    def deduction_pct(self, form: str) -> float:
        c = self.settings.all()
        return c.get(f"gold_buyback_deduction_{form}_pct", c["gold_buyback_deduction_jewellery_pct"])

    def value_item(self, item: GoldItem, price: dict, realised: te.Gains, today: dt.date) -> dict:
        rate = self.rate_per_g(item, price)
        fine = self.fineness(item.purity)
        d = {
            "id": item.id, "date": item.date.isoformat(), "description": item.description, "form": item.form,
            "purity": item.purity, "fineness": fine, "weight_g": item.weight_g, "fine_weight_g": _r(item.weight_g * fine, 3),
            "amount_paid": _r(item.amount_paid), "making_charges": _r(item.making_charges), "gst_paid": _r(item.gst_paid),
            "seller": item.seller, "remarks": item.remarks, "row": item.row,
            "sold_date": item.sold_date.isoformat() if item.sold_date else None, "sold_amount": _r(item.sold_amount),
            "photos": [{"id": p["id"], "filename": p["filename"]} for p in self.repo.gold_photos(item.id)],
            "lt_date": self.lt_date(item).isoformat(),
        }
        if item.sold_date:
            gain = (item.sold_amount or 0) - item.amount_paid
            lt = item.sold_date > self.lt_date(item)
            d.update(status="sold", gain=_r(gain), term="LT" if lt else "ST",
                     days_held=(item.sold_date - item.date).days)
            return d
        days = (today - item.date).days
        lt = today > self.lt_date(item)
        d.update(status="held", days_held=days, term="LT" if lt else "ST",
                 days_to_lt=max(0, (self.lt_date(item) - today).days + 1) if not lt else 0)
        if rate is None:
            d.update(rate_per_g=None, market_value=None)
            return d
        market = rate * item.weight_g
        ded_pct = self.deduction_pct(item.form)
        sale = market * (1 - ded_pct / 100)
        gain = sale - item.amount_paid
        g = te.Gains(lt_other=gain) if lt else te.Gains(st_slab=gain)
        rules = self.rules()
        comp = te.compute_tax(realised + g, rules)
        tax = comp.total - te.tax_total(realised, rules)
        d.update(
            rate_per_g=_r(rate), market_value=_r(market), buyback_deduction_pct=ded_pct,
            buyback_deduction=_r(market - sale), sale_value=_r(sale), gain=_r(gain),
            gain_pct=_r(gain / item.amount_paid * 100 if item.amount_paid else None), tax_if_sold=_r(tax),
            net_if_sold=_r(sale - tax), after_tax_gain=_r(sale - tax - item.amount_paid),
            bucket="lt_other" if lt else "st_slab",
            rate_pct=rules.ltcg_pct if lt else rules.slab_pct,
            paid_per_g=_r(item.amount_paid / item.weight_g if item.weight_g else None),
            computation=comp.as_dict(),
        )
        if not lt and gain > 0:
            later = te.compute_tax(realised + te.Gains(lt_other=gain), rules).total - te.tax_total(realised, rules)
            d["if_wait"] = {"date": (self.lt_date(item) + dt.timedelta(days=1)).isoformat(), "tax": _r(later),
                            "saved": _r(tax - later)}
        return d

    def overview(self) -> dict:
        today = dt.date.today()
        c = self.settings.all()
        price = self.price()
        items = self.portfolio.state().extras.gold
        # tax on other gains already booked this FY (shares + earlier gold sales) is the baseline
        realised = self.tax.realised(today)[0] if self.tax is not None else te.Gains()
        rows = [self.value_item(i, price, realised, today) for i in items]
        held = [r for r in rows if r["status"] == "held"]
        priced = [r for r in held if r.get("market_value") is not None]
        tot = lambda k, rs=priced: _r(sum(r.get(k) or 0 for r in rs))  # noqa: E731
        totals = {
            "items": len(held), "weight_g": _r(sum(r["weight_g"] for r in held), 3),
            "fine_weight_g": _r(sum(r["fine_weight_g"] for r in held), 3),
            "invested": _r(sum(r["amount_paid"] for r in held)), "market_value": tot("market_value"),
            "sale_value": tot("sale_value"), "gain": tot("gain"), "tax_if_sold": None, "net_if_sold": None,
        }
        if priced:
            g = te.Gains(lt_other=sum(r["gain"] for r in priced if r["term"] == "LT"),
                         st_slab=sum(r["gain"] for r in priced if r["term"] == "ST"))
            rules = self.rules()
            comp = te.compute_tax(realised + g, rules)
            tax = comp.total - te.tax_total(realised, rules)
            totals.update(tax_if_sold=_r(tax), net_if_sold=_r(totals["sale_value"] - tax),
                          after_tax_gain=_r(totals["sale_value"] - tax - sum(r["amount_paid"] for r in priced)),
                          computation=comp.as_dict())
        by_form = {}
        for r in held:
            f = by_form.setdefault(r["form"], {"form": r["form"], "items": 0, "weight_g": 0.0, "invested": 0.0, "market_value": 0.0})
            f["items"] += 1
            f["weight_g"] = _r(f["weight_g"] + r["weight_g"], 3)
            f["invested"] = _r(f["invested"] + r["amount_paid"])
            f["market_value"] = _r(f["market_value"] + (r.get("market_value") or 0))
        public_price = {k: v for k, v in price.items() if not k.startswith("_")}
        return {
            "price": public_price, "items": rows, "totals": totals, "by_form": list(by_form.values()),
            "settings": {k: c[k] for k in c if k.startswith("gold_")} | {"slab_rate_pct": c["slab_rate_pct"],
                                                                       "cess_pct": c["cess_pct"], "surcharge_pct": c["surcharge_pct"]},
            "realised_this_fy": self.realised_detail(today), "fy": fy_label(today),
            "notes": [
                f"Long term after {c['gold_lt_months']} months: {c['gold_ltcg_rate_pct']}% without indexation; "
                f"otherwise added to income at your slab ({c['slab_rate_pct']}%); plus {c['cess_pct']}% cess.",
                "Cost = everything you paid (gold + making charges + GST). Making charges and GST are not recovered when selling.",
                f"Sale value = market rate minus the buy-back deduction (coin {c['gold_buyback_deduction_coin_pct']}%, "
                f"bar {c['gold_buyback_deduction_bar_pct']}%, jewellery {c['gold_buyback_deduction_jewellery_pct']}%).",
                f"GST when buying: {c['gold_gst_pct']}% on gold value, {c['gold_making_gst_pct']}% on making charges. "
                "No GST is charged to you when you sell to a jeweller.",
            ],
        }

    # ---------------------------------------------------------- realised (tax)
    def _sold_in_fy(self, today: dt.date) -> list[GoldItem]:
        try:
            items = self.portfolio.state().extras.gold
        except Exception:
            return []
        s, e = fy_bounds(today)
        return [i for i in items if i.sold_date and s <= i.sold_date <= e and i.sold_amount is not None]

    def realised_gains(self, today: dt.date) -> te.Gains:
        g = te.Gains()
        for i in self._sold_in_fy(today):
            gain = i.sold_amount - i.amount_paid
            g.add("lt_other" if i.sold_date > self.lt_date(i) else "st_slab", gain)
        return g

    def realised_detail(self, today: dt.date) -> list[dict]:
        return [{"id": i.id, "description": i.description, "sold_date": i.sold_date.isoformat(),
                 "sold_amount": _r(i.sold_amount), "cost": _r(i.amount_paid), "gain": _r(i.sold_amount - i.amount_paid),
                 "term": "LT" if i.sold_date > self.lt_date(i) else "ST"} for i in self._sold_in_fy(today)]

    # ------------------------------------------------------------------ CRUD
    def _validate(self, g: GoldItem) -> GoldItem:
        g.form = (g.form or "").lower()
        if g.form not in FORMS:
            raise ValueError("Form must be coin, bar or jewellery")
        if g.weight_g <= 0 or g.amount_paid <= 0:
            raise ValueError("Weight and amount paid must be greater than zero")
        if g.date > dt.date.today():
            raise ValueError("Purchase date cannot be in the future")
        if g.making_charges < 0 or g.gst_paid < 0:
            raise ValueError("Making charges and GST cannot be negative")
        if g.making_charges + g.gst_paid > g.amount_paid:
            raise ValueError("Making charges + GST cannot exceed the amount paid (they are part of it)")
        if g.sold_date and (g.sold_amount is None or g.sold_amount < 0):
            raise ValueError("Enter the amount you received when selling")
        if g.sold_date and g.sold_date < g.date:
            raise ValueError("Sale date is before the purchase date")
        if g.sold_date and g.sold_date > dt.date.today():
            raise ValueError("Sale date cannot be in the future")
        if not g.sold_date:
            g.sold_amount = None
        f = self.fineness(g.purity)
        if not 0 < f <= 1:
            raise ValueError("Unknown purity")
        g.purity = g.purity.strip().upper()
        return g

    def _commit(self, items: list[GoldItem], message: str) -> dict:
        with self.versions.write_lock:
            data = self.versions.head_bytes()
            new = self.extras.write(data, gold=self._with_values(items))
            self.versions.commit(new, message=message, source="gold")
        return self.overview()

    def _with_values(self, items: list[GoldItem], price: dict | None = None) -> list[GoldItem]:
        """Fill rate/value columns so the Gold sheet is readable on its own."""
        try:
            price = price or self.price()
        except Exception:
            price = {}
        today = dt.date.today()
        for i in items:
            if i.sold_date:
                i.rate_per_g = i.current_value = i.value_date = None
                continue
            r = self.rate_per_g(i, price) if price else None
            if r:
                i.rate_per_g = round(r, 2)
                i.current_value = round(r * i.weight_g, 2)
                i.value_date = today
        return items

    def add(self, g: GoldItem) -> dict:
        items = self.portfolio.state().extras.gold
        g.id = g.id or new_id("G")
        self._validate(g)
        return self._commit(items + [g], f"Added gold: {g.description} ({g.weight_g:g} g {g.purity})")

    def update(self, item_id: str, g: GoldItem) -> dict:
        items = self.portfolio.state().extras.gold
        if not any(i.id == item_id for i in items):
            raise KeyError(item_id)
        g.id = item_id
        self._validate(g)
        return self._commit([g if i.id == item_id else i for i in items], f"Edited gold: {g.description}")

    def delete(self, item_id: str) -> dict:
        items = self.portfolio.state().extras.gold
        keep = [i for i in items if i.id != item_id]
        if len(keep) == len(items):
            raise KeyError(item_id)
        gone = next(i for i in items if i.id == item_id)
        out = self._commit(keep, f"Removed gold: {gone.description}")
        # photos only after the sheet write succeeded (they are not versioned)
        for p in self.repo.gold_photos(item_id):
            self.delete_photo(p["id"])
        return out

    # --------------------------------------------------------------- photos
    def add_photo(self, item_id: str, data: bytes, filename: str | None) -> dict:
        if not any(i.id == item_id for i in self.portfolio.state().extras.gold):
            raise KeyError(item_id)
        if len(data) > MAX_PHOTO:
            raise ValueError("Photo is larger than 8 MB")
        mime = sniff_image(data)
        if not mime:
            raise ValueError("Only JPEG, PNG, WebP, GIF or HEIC photos are accepted")
        if len(self.repo.gold_photos(item_id)) >= 10:
            raise ValueError("At most 10 photos per item")
        sha = self.photos.put(data)
        pid = self.repo.add_gold_photo(item_id, sha, mime, (filename or "photo")[:120], len(data))
        self.repo.audit("gold.photo.add", {"item": item_id, "photo": pid})
        return {"id": pid}

    def photo(self, photo_id: int) -> tuple[bytes, str, str]:
        p = self.repo.gold_photo(photo_id)
        if not p:
            raise KeyError(photo_id)
        return self.photos.get(p["sha256"]), p["mime"], p["filename"] or "photo"

    def delete_photo(self, photo_id: int) -> None:
        p = self.repo.gold_photo(photo_id)
        if not p:
            raise KeyError(photo_id)
        self.repo.delete_gold_photo(photo_id)
        if not self.repo.photo_sha_in_use(p["sha256"]):
            self.photos.delete(p["sha256"])

    # ------------------------------------------------------- price sync hook
    def refresh_sheet_values(self, data: bytes) -> bytes | None:
        """Called by the price sync (under the sheet lock): rewrite the Gold sheet's
        rate/value columns. Returns new bytes, or None when nothing changed."""
        items = self.extras.read(data).gold
        held = [i for i in items if not i.sold_date]
        if not held:
            return None
        price = self.price()
        if price.get("source") not in ("live", "manual"):
            return None
        before = [(i.rate_per_g, i.current_value) for i in held]
        self._with_values(items, price)
        after = [(i.rate_per_g, i.current_value) for i in held]
        if all(a and b and abs((a[0] or 0) - (b[0] or 0)) < 0.005 for a, b in zip(before, after)):
            return None
        return self.extras.write(data, gold=items)
