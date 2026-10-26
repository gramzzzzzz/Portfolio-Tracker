"""Tax planning built on the pure engine in ``tax_engine``.

Turns the sheet snapshot + live prices into tax lots and answers:
  * where do I stand this FY (realised + everything-sold-today, step-by-step);
  * what does selling N units / ₹X of a holding leave me with, before and after tax,
    and what if I wait until those units turn long term;
  * how do I raise ₹X in hand with the least tax;
  * what does a monthly withdrawal plan look like over years (tax, depletion);
  * harvesting: tax-free LTCG booking, loss booking, lots about to turn long term.
"""
from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict

from app.dao.repo.sqlite_repo import Repository
from app.domain.models import SheetSnapshot
from app.services import tax_engine as te
from app.services.analytics_service import tax_class
from app.services.finance import add_months, fy_bounds, fy_label
from app.services.settings_service import SettingsService

def assumptions(c: dict) -> list[str]:
    return [
        "Units are sold oldest-first (FIFO), as the depository debits them.",
        f"Held more than {int(c['equity_lt_months'])} months = long term (listed shares, equity funds/ETFs, gold ETFs).",
        "STT is excluded from the cost of acquisition and is not deductible on sale; brokerage, exchange, "
        "SEBI, GST and DP charges on the sale are deducted from the sale value.",
        f"Purchase STT comes from imported contract notes; for rows typed in by hand it is estimated at "
        f"{c['stt_buy_share_pct']}% of the purchase value for shares (0 for ETFs / funds).",
        "Equity LTCG exemption is applied after set-off of losses and resets every 1 April.",
        "Not modelled: losses carried forward from earlier years, grandfathering of pre-2018 purchases, "
        "the basic-exemption-limit adjustment and rebates. This is a planning aid, not tax advice.",
    ]



def _r(x, n=2):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(x, n) + 0.0  # no "-0"


class TaxService:
    def __init__(self, repo: Repository, analytics, settings: SettingsService, portfolio=None):
        self.repo = repo
        self.analytics = analytics
        self.settings = settings
        self.portfolio = portfolio  # PortfolioService: recorded sales -> realised gains
        self.gold = None  # GoldService (attached by the container): gold sales -> realised gains

    # ----------------------------------------------------------------- inputs
    def rules(self) -> te.TaxRules:
        c = self.settings.all()
        return te.TaxRules(stcg_equity_pct=c["stcg_rate_pct"], ltcg_pct=c["ltcg_rate_pct"],
                           ltcg_exemption=c["ltcg_exemption"], slab_pct=c["slab_rate_pct"],
                           surcharge_pct=c["surcharge_pct"], cess_pct=c["cess_pct"])

    def observed_expense_pct(self) -> float | None:
        """Deductible charges (excl. STT and stamp duty) as % of traded value, from imported notes."""
        num = den = 0.0
        for n in self.repo.notes():
            ch = n["charges"]
            ded = sum(ch.get(k) or 0 for k in ("brokerage", "exchange_txn", "sebi_fees", "cgst", "sgst", "igst", "utt"))
            total = sum(v or 0 for v in ch.values())
            turnover = abs((n["net_payable"] or 0) - total)
            if turnover > 0:
                num += ded
                den += turnover
        return num / den * 100 if den else None

    def costs(self) -> te.SellCosts:
        c = self.settings.all()
        pct = c["sell_expense_pct"]
        if pct is None:
            obs = self.observed_expense_pct()
            pct = obs if obs is not None else 0.01
        return te.SellCosts(expense_pct=pct, dp_charge=c["dp_charge_per_sale"], stt_share_pct=c["stt_sell_share_pct"],
                            stt_fund_pct=c["stt_sell_fund_pct"])

    def manual_realised(self, today: dt.date) -> tuple[te.Gains, bool]:
        """Gains booked this FY *outside* the app (other brokers, sales not recorded in the
        Transactions sheet). Entered by the user; ignored once that FY is over."""
        c = self.settings.all()
        current = c.get("realized_fy") == fy_label(today)
        if not current:
            return te.Gains(), False
        return te.Gains(st_equity=c["realized_stcg_fy"], lt_equity=c["realized_ltcg_fy"],
                        st_slab=c["realized_st_other_fy"], lt_other=c["realized_lt_other_fy"]), True

    def recorded_sales(self, fy_of: dt.date | None = None) -> list[dict]:
        """Every recorded sale (Transactions sheet), split into FIFO legs with tax treatment.
        ``fy_of``: only sales in the financial year containing that date."""
        if self.portfolio is None:
            return []
        try:
            st = self.portfolio.state()
        except Exception:
            return []
        cfg = self.settings.all()
        ledger_stt = self._purchase_stt()
        rows = {l.row: l for l in st.sheet.lots}
        window = fy_bounds(fy_of) if fy_of else None
        out = []
        for sale in st.positions.sales:
            t = sale.txn
            if window and not (window[0] <= t.date <= window[1]):
                continue
            src = rows.get(sale.legs[0].lot_row) if sale.legs else None
            src = src or next((l for l in st.sheet.lots if l.name == sale.name), None)
            tclass = tax_class(src.sector if src else None, sale.name, cfg["tax_class_by_sector"])
            kind = self.kind_of(src, tclass) if src else "share"
            units = sale.units or 1.0
            legs, gains = [], te.Gains()
            for g in sale.legs:
                share = g.units / units
                proceeds = (t.price or 0) * g.units
                expenses = (t.charges or 0) * share
                stt_sell = (t.stt or 0) * share
                if g.origin == "bonus":
                    stt_buy, cost = 0.0, 0.0
                else:
                    row_qty = g.orig_qty or 0
                    row_cost = g.orig_cost or 0
                    full = ledger_stt.get((sale.name, g.purchase_date.isoformat(), round(row_qty, 6)))
                    if full is None:
                        full = round(max(0.0, row_cost - (g.orig_charges or 0)) * cfg["stt_buy_share_pct"] / 100, 2) \
                            if kind == "share" else 0.0
                    stt_buy = full * (g.sheet_cost / row_cost) if row_cost else 0.0
                    cost = g.sheet_cost - stt_buy
                lt = t.date > add_months(g.purchase_date, int(cfg["equity_lt_months"]))
                bucket = te.bucket_for(tclass, "LT" if lt else "ST")
                gain = proceeds - cost - expenses
                gains.add(bucket, gain)
                legs.append({"row": g.lot_row if g.origin == "purchase" else None, "origin": g.origin,
                             "purchase_date": g.purchase_date.isoformat(), "units": g.units, "proceeds": _r(proceeds),
                             "cost": _r(cost), "stt_buy": _r(stt_buy), "expenses": _r(expenses), "stt_sell": _r(stt_sell),
                             "gain": _r(gain), "term": "LT" if lt else "ST", "bucket": bucket,
                             "bucket_label": te.BUCKET_SHORT[bucket]})
            out.append({"id": t.id, "date": t.date.isoformat(), "fy": fy_label(t.date), "name": sale.name,
                        "units": sale.units, "price": t.price, "proceeds": _r((t.price or 0) * sale.units),
                        "charges": _r(t.charges), "stt": _r(t.stt), "gain": _r(gains.total), "gains": gains.as_dict(),
                        "unmatched_units": sale.unmatched_units, "legs": legs, "source": t.source,
                        "note_no": t.note_no, "asset_class": t.asset_class})
        return out

    def realised(self, today: dt.date) -> tuple[te.Gains, bool]:
        """All gains booked this FY: recorded share sales + gold sales + manual entries."""
        manual, current = self.manual_realised(today)
        total = te.Gains(**manual.as_dict())
        for s in self.recorded_sales(today):
            total = total + te.Gains(**s["gains"])
        if self.gold is not None:
            total = total + self.gold.realised_gains(today)
        return total, current

    def _purchase_stt(self) -> dict[tuple, float]:
        out = {}
        for r in self.repo.ledger():
            if r["status"] == "recorded" and r["side"] == "BUY":
                out[(r["sheet_name"], r["trade_date"], round(r["qty"] or 0, 6))] = r["stt"] or 0.0
        return out

    @staticmethod
    def kind_of(lot, tclass: str) -> str:
        return "share" if (lot.section == 0 and tclass == te.EQUITY) else ("equity_fund" if tclass == te.EQUITY else "other")

    def purchase_stt(self, lot, kind: str, ledger_stt: dict, fraction: float | None = None) -> float:
        """Purchase STT attributable to (a fraction of) a lot.

        The STT of the whole purchase row comes from the import ledger, or is estimated as 0.1%
        of the row's gross value for shares (0 for ETFs/funds). It is then pro-rated by the share
        of the row's cost still in this lot (``lot.cost / orig_cost``) - correct after partial
        sells and splits - or by ``fraction`` of the lot when given. Bonus lots: 0."""
        if getattr(lot, "origin", "purchase") == "bonus":
            return 0.0
        row_qty = lot.orig_qty if lot.orig_qty is not None else lot.qty
        row_cost = lot.orig_cost if lot.orig_cost is not None else lot.cost
        full = ledger_stt.get((lot.name, lot.date.isoformat(), round(row_qty, 6)))
        if full is None:
            row_charges = lot.orig_charges if lot.orig_charges is not None else lot.charges
            full = round(max(0.0, row_cost - row_charges) * self.settings.get("stt_buy_share_pct") / 100, 2) \
                if kind == "share" else 0.0
        share = (lot.cost / row_cost) if row_cost else 0.0
        if fraction is not None:
            share *= fraction
        return round(full * share, 4)

    def lots(self, snap: SheetSnapshot, views=None) -> tuple[dict[str, list[te.TaxLot]], list[dict], list]:
        """FIFO-ordered tax lots per holding + lots that cannot be taxed (no date/price)."""
        views = views if views is not None else self.analytics.price_lots(snap)[0]
        ledger_stt = self._purchase_stt()
        lt_months = int(self.settings.get("equity_lt_months"))
        by_name: dict[str, list[te.TaxLot]] = defaultdict(list)
        excluded = []
        for v in views:
            l = v.lot
            if not l.date or not v.price or l.held_qty <= 0:
                excluded.append({"row": l.row, "name": l.name,
                                 "reason": "no purchase date" if not l.date else "no price" if not v.price else "no units"})
                continue
            kind = self.kind_of(l, v.tclass)
            stt = self.purchase_stt(l, kind, ledger_stt)
            by_name[l.name].append(te.TaxLot(
                key=str(l.row), name=l.name, date=l.date, units=l.held_qty, cost=max(0.0, l.cost - stt),
                sheet_cost=l.cost, stt_buy=stt, price=v.price, tclass=v.tclass, kind=kind,
                fractional=not te.is_whole(l.held_qty), lt_months=lt_months))
        for name, ls in by_name.items():
            ls.sort(key=lambda x: (x.date, int(x.key)))
            frac = any(x.fractional for x in ls)
            for x in ls:
                x.fractional = frac
        return dict(by_name), excluded, views

    # ------------------------------------------------------------ liquidation
    def liquidation(self, snap: SheetSnapshot, views=None) -> dict:
        """Cash in hand if everything (and each holding) were sold today."""
        today = dt.date.today()
        rules, costs = self.rules(), self.costs()
        realised, _ = self.realised(today)
        holdings, _, _ = self.lots(snap, views)
        per = {}
        for name, ls in holdings.items():
            res = te.evaluate(te.make_legs(ls, sum(x.units for x in ls), today, costs), realised, rules)
            per[name] = {"tax_if_sold": _r(res.tax), "exit_costs": _r(res.expenses + res.stt), "after_tax_value": _r(res.net),
                         "after_tax_gain": _r(res.net - sum(x.sheet_cost for x in ls))}
        legs = [l for name, ls in holdings.items() for l in te.make_legs(ls, sum(x.units for x in ls), today, costs)]
        total = te.evaluate(legs, realised, rules)
        cost = sum(x.sheet_cost for ls in holdings.values() for x in ls)
        return {"holdings": per, "total": {
            "tax_if_sold": _r(total.tax), "exit_costs": _r(total.expenses + total.stt),
            "expenses": _r(total.expenses), "stt": _r(total.stt),
            "after_tax_value": _r(total.net), "after_tax_gain": _r(total.net - cost)}, "sale": total}

    # ---------------------------------------------------------------- summary
    def summary(self, snap: SheetSnapshot) -> dict:
        today = dt.date.today()
        c = self.settings.all()
        rules, costs = self.rules(), self.costs()
        realised, realised_current = self.realised(today)
        holdings, excluded, views = self.lots(snap)
        liq = self.liquidation(snap, views)
        sale: te.SaleResult = liq["sale"]
        fy_s, fy_e = fy_bounds(today)

        lot_rows = []
        for name, ls in holdings.items():
            older = 0.0
            for lot in ls:
                legs = te.make_legs([lot], lot.units, today, costs)
                alone = te.evaluate(legs, realised, rules)
                leg = legs[0]
                lot_rows.append({
                    "row": int(lot.key), "bonus": int(lot.key) >= 1_000_000, "name": name, "date": lot.date.isoformat(), "units": lot.units,
                    "price": _r(lot.price), "value": _r(leg.proceeds), "cost": _r(lot.cost), "sheet_cost": _r(lot.sheet_cost),
                    "stt_buy": _r(lot.stt_buy), "exit_costs": _r(leg.expenses + leg.stt), "gain": _r(leg.gain),
                    "term": leg.term, "lt_date": lot.lt_date().isoformat(),
                    "days_to_lt": max(0, (lot.lt_date() - today).days + 1) if leg.term == "ST" else 0,
                    "tax_class": lot.tclass, "bucket": leg.bucket, "bucket_label": te.BUCKET_SHORT[leg.bucket],
                    "rate_pct": _r(rules.rate(leg.bucket) * 100), "tax_if_sold": _r(alone.tax),
                    "after_tax_gain": _r(leg.gain - alone.tax), "older_units_first": older,
                })
                older += lot.units

        unreal = te.gains_of(sale.legs)
        return {
            "fy": fy_label(today), "fy_start": fy_s.isoformat(), "fy_end": fy_e.isoformat(),
            "days_left_in_fy": (fy_e - today).days, "today": today.isoformat(),
            "rules": {"stcg_equity_pct": rules.stcg_equity_pct, "ltcg_pct": rules.ltcg_pct,
                      "ltcg_exemption": rules.ltcg_exemption, "slab_pct": rules.slab_pct,
                      "surcharge_pct": rules.surcharge_pct, "cess_pct": rules.cess_pct},
            "costs": {"expense_pct": _r(costs.expense_pct, 4), "expense_source": "setting" if c["sell_expense_pct"] is not None
                      else ("your contract notes" if self.observed_expense_pct() is not None else "default"),
                      "dp_charge": costs.dp_charge, "stt_share_pct": costs.stt_share_pct, "stt_fund_pct": costs.stt_fund_pct},
            "realised": {**realised.as_dict(), "current": realised_current, "fy": c.get("realized_fy"),
                         "manual": self.manual_realised(today)[0].as_dict(),
                         "sales": self.recorded_sales(today),
                         "gold": self.gold.realised_detail(today) if self.gold is not None else []},
            "realised_tax": te.compute_tax(realised, rules).as_dict(),
            "unrealised": unreal.as_dict(),
            "position": {
                "value": _r(sale.proceeds), "cost": _r(sum(x.sheet_cost for ls in holdings.values() for x in ls)),
                "gain_pre_tax": _r(unreal.total), **liq["total"], "computation": sale.computation.as_dict(),
                "tax_before": _r(sale.tax_before), "tax_after": _r(sale.tax_after),
            },
            "holdings": [{"name": n, "units": sum(x.units for x in ls), **liq["holdings"][n],
                          "lt_units": sum(x.units for x in ls if x.term_on(today) == "LT"),
                          "st_units": sum(x.units for x in ls if x.term_on(today) == "ST"),
                          "price": _r(ls[0].price)} for n, ls in sorted(holdings.items())],
            "lots": sorted(lot_rows, key=lambda x: (x["name"], x["date"])),
            "excluded": excluded,
            "harvest": self._harvest(holdings, realised, rules, costs, today, snap),
            "loss_harvest": self._loss_harvest(holdings, realised, rules, costs, today),
            "upcoming_lt": self._upcoming(holdings, realised, rules, costs, today, int(c["upcoming_lt_window_days"])),
            "assumptions": assumptions(c),
        }

    def _harvest(self, holdings, realised: te.Gains, rules, costs, today, snap) -> dict:
        """Book long-term equity gains up to the unused exemption (sell + buy back) so the
        cost basis resets and that gain is never taxed. FIFO: only the oldest LT lots qualify."""
        base = te.compute_tax(realised, rules)
        headroom = max(0.0, rules.ltcg_exemption - base.after_setoff["lt_equity"])
        gross = sum(l.qty * l.rate for l in snap.lots) or 1.0
        buy_pct = sum(l.charges for l in snap.lots) / gross * 100
        plan, left, state = [], headroom, te.Gains(**realised.as_dict())
        cands = []
        for name, ls in holdings.items():
            prefix = []
            for lot in ls:
                if lot.tclass != te.EQUITY or lot.term_on(today) != "LT":
                    break
                prefix.append(lot)
            if prefix:
                g = sum(te.make_legs([x], x.units, today, costs, include_dp=False)[0].gain for x in prefix)
                cands.append((g / (sum(x.units * x.price for x in prefix) or 1), name, prefix))
        for _, name, prefix in sorted(cands, key=lambda c: -c[0]):
            if left < 1:
                break
            units = 0.0
            for lot in prefix:
                per_unit = te.make_legs([lot], 1 if not lot.fractional else 0.001, today, costs, include_dp=False)[0].gain
                per_unit = per_unit / (1 if not lot.fractional else 0.001)
                if per_unit <= 0:
                    units += lot.units
                    continue
                cur = te.gains_of(te.make_legs(prefix, units, today, costs, include_dp=False)).total if units else 0
                can = max(0.0, (left - cur) / per_unit)
                can = math.floor(can) if not lot.fractional else math.floor(can * 1000) / 1000
                take = min(lot.units, can)
                units += take
                if take < lot.units:
                    break
            if units <= 0:
                continue
            legs = te.make_legs(prefix, units, today, costs)
            res = te.evaluate(legs, state, rules)
            gain = res.gains.total
            if gain <= 0:
                continue
            rebuy = res.proceeds * buy_pct / 100
            saved = gain * rules.ltcg_pct / 100 * rules.multiplier
            plan.append({"name": name, "units": units, "price": _r(legs[0].price), "sell_value": _r(res.proceeds),
                         "gain": _r(gain), "tax_now": _r(res.tax), "future_tax_saved": _r(saved),
                         "round_trip_costs": _r(res.expenses + res.stt + rebuy), "net_benefit": _r(saved - res.expenses - res.stt - rebuy - res.tax)})
            state = state + res.gains
            left -= gain
        return {"headroom": _r(headroom), "plan": plan, "total_gain": _r(sum(p["gain"] for p in plan)),
                "total_future_tax_saved": _r(sum(p["future_tax_saved"] for p in plan)),
                "buy_charges_pct": _r(buy_pct, 3)}

    def _loss_harvest(self, holdings, realised, rules, costs, today) -> list[dict]:
        """Oldest lots currently at a loss: booking them offsets gains realised this FY now, or is
        carried forward. FIFO: a loss lot can only be sold after the older units of that security."""
        out = []
        for name, ls in holdings.items():
            prefix, units = [], 0.0
            for lot in ls:
                leg = te.make_legs([lot], lot.units, today, costs, include_dp=False)[0]
                if leg.gain >= 0:
                    break
                prefix.append(lot)
                units += lot.units
            if not prefix:
                continue
            res = te.evaluate(te.make_legs(prefix, units, today, costs), realised, rules)
            comp = res.computation
            out.append({"name": name, "units": units, "loss": _r(res.gains.total), "gains": res.gains.as_dict(),
                        "tax_saved_this_fy": _r(-res.tax), "carried_forward": comp.carried_forward,
                        "terms": sorted({l.term for l in res.legs})})
        out.sort(key=lambda x: x["loss"])
        return out

    def _upcoming(self, holdings, realised, rules, costs, today, window) -> list[dict]:
        out = []
        for name, ls in holdings.items():
            for lot in ls:
                if lot.term_on(today) != "ST":
                    continue
                becomes = lot.lt_date() + dt.timedelta(days=1)
                if (becomes - today).days > window:
                    continue
                now = te.evaluate(te.make_legs([lot], lot.units, today, costs), realised, rules)
                if now.gains.total <= 0:
                    continue
                later = te.evaluate(te.make_legs([lot], lot.units, becomes, costs), realised, rules)
                out.append({"row": int(lot.key), "name": name, "date": lot.date.isoformat(), "units": lot.units,
                            "gain": _r(now.gains.total), "becomes_lt": becomes.isoformat(),
                            "days": (becomes - today).days, "tax_now": _r(now.tax), "tax_if_waiting": _r(later.tax),
                            "saved_by_waiting": _r(now.tax - later.tax)})
        return sorted(out, key=lambda x: x["days"])

    # -------------------------------------------------------------- what-ifs
    def simulate_sale(self, snap: SheetSnapshot, name: str, *, units: float | None = None,
                      amount: float | None = None, sale_date: dt.date | None = None,
                      price: float | None = None) -> dict:
        today = dt.date.today()
        sale_date = sale_date or today
        if sale_date < today:
            raise ValueError("Sale date cannot be in the past")
        rules, costs = self.rules(), self.costs()
        holdings, _, _ = self.lots(snap)
        if name not in holdings:
            raise ValueError(f"'{name}' has no priced, dated lots in the sheet")
        ls = holdings[name]
        held = sum(x.units for x in ls)
        p = price if price and price > 0 else ls[0].price
        frac = ls[0].fractional
        if units is None:
            if not amount or amount <= 0:
                raise ValueError("Enter units or an amount")
            units = amount / p
            units = math.floor(units * 1000) / 1000 if frac else math.floor(units)
        if units <= 0:
            raise ValueError("That amount is less than one unit")
        if units > held + 1e-9:
            raise ValueError(f"You hold only {held:g} units of {name}")
        realised = self.realised(today)[0] if te.fy_window(sale_date) == te.fy_window(today) else te.Gains()
        res = te.evaluate(te.make_legs(ls, units, sale_date, costs, price=p), realised, rules)
        out = {"name": name, "units": units, "held": held, "price": _r(p), "sale_date": sale_date.isoformat(),
               "fy": fy_label(sale_date), **res.as_dict(),
               "cost_basis": _r(sum(l.cost for l in res.legs)),
               "sheet_cost": _r(sum(x.sheet_cost * l.units / x.units for l in res.legs for x in ls if x.key == l.key))}
        out["after_tax_gain"] = _r(out["net"] - out["sheet_cost"])
        wait = te.wait_for_lt(ls, units, sale_date)
        if wait:
            r2 = self.realised(today)[0] if te.fy_window(wait) == te.fy_window(today) else te.Gains()
            later = te.evaluate(te.make_legs(ls, units, wait, costs, price=p), r2, rules)
            out["if_wait"] = {"date": wait.isoformat(), "days": (wait - sale_date).days, "tax": _r(later.tax),
                              "net": _r(later.net), "saved": _r(res.tax - later.tax), "fy": fy_label(wait)}
        return out

    def raise_cash(self, snap: SheetSnapshot, target_net: float, exclude: list[str] | None = None) -> dict:
        if target_net <= 0:
            raise ValueError("Enter the amount you need")
        today = dt.date.today()
        holdings, _, _ = self.lots(snap)
        plan = te.raise_cash(holdings, target_net, today, self.realised(today)[0], self.rules(), self.costs(),
                             set(exclude or []))
        plan.pop("_remaining", None)
        total_value = sum(x.units * x.price for ls in holdings.values() for x in ls)
        plan["portfolio_value"] = _r(total_value)
        return plan

    def withdrawal_plan(self, snap: SheetSnapshot, monthly_net: float, months: int, annual_return_pct: float,
                        start: dt.date | None = None, exclude: list[str] | None = None) -> dict:
        if monthly_net <= 0 or not 1 <= months <= 600:
            raise ValueError("Enter a monthly amount and 1–600 months")
        today = dt.date.today()
        start = start or add_months(dt.date(today.year, today.month, 1), 1)
        holdings, _, _ = self.lots(snap)
        out = te.withdrawal_plan(holdings, monthly_net, months, annual_return_pct, start, self.realised(today)[0],
                                 self.rules(), self.costs(), set(exclude or []))
        out.update(start=start.isoformat(), monthly_net=monthly_net, annual_return_pct=annual_return_pct,
                   starting_value=_r(sum(x.units * x.price for ls in holdings.values() for x in ls)))
        return out
