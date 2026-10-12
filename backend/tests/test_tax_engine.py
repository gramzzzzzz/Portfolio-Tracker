"""Hand-calculated checks of the capital-gains engine (FY 2026-27 rules)."""
import datetime as dt

import pytest

from app.services import tax_engine as te
from app.services.tax_engine import Gains, SellCosts, TaxLot, TaxRules

R = TaxRules()  # 20% / 12.5% / 1.25L / slab 30% / cess 4%
TODAY = dt.date(2026, 10, 12)


def lot(key, name, date, units, unit_cost, price, tclass="equity", kind="share"):
    return TaxLot(key=key, name=name, date=date, units=units, cost=units * unit_cost, sheet_cost=units * unit_cost,
                  stt_buy=0.0, price=price, tclass=tclass, kind=kind)


def test_stcg_equity_with_cess():
    c = te.compute_tax(Gains(st_equity=10000), R)
    assert c.base_total == 2000 and c.cess == 80 and c.total == 2080


def test_ltcg_exemption():
    c = te.compute_tax(Gains(lt_equity=200000), R)
    assert c.exemption_applied == 125000 and c.taxable["lt_equity"] == 75000
    assert c.total == pytest.approx(75000 * 0.125 * 1.04)  # 9750


def test_short_term_loss_offsets_long_term_gain():
    c = te.compute_tax(Gains(st_equity=-50000, lt_equity=200000), R)
    assert c.after_setoff["lt_equity"] == 150000
    assert c.total == pytest.approx(25000 * 0.125 * 1.04)  # 3250


def test_long_term_loss_cannot_offset_short_term_gain():
    c = te.compute_tax(Gains(st_equity=10000, lt_equity=-20000), R)
    assert c.total == 2080 and c.carried_forward == {"st": 0, "lt": 20000}


def test_losses_go_where_they_save_most():
    # ST loss must hit the 30% slab bucket before the 12.5% LT bucket
    c = te.compute_tax(Gains(st_equity=-5000, st_slab=10000, lt_other=10000), R)
    assert c.after_setoff == {"st_equity": 0, "lt_equity": 0, "st_slab": 5000, "lt_other": 10000}
    assert c.total == pytest.approx((5000 * 0.30 + 10000 * 0.125) * 1.04)  # 2860


def test_surcharge_then_cess():
    c = te.compute_tax(Gains(st_equity=100000), TaxRules(surcharge_pct=15))
    assert c.surcharge == 3000 and c.cess == pytest.approx(23000 * 0.04) and c.total == pytest.approx(23920)


def test_steps_explain_every_number():
    c = te.compute_tax(Gains(st_equity=-50000, lt_equity=200000), R)
    sections = [s["section"] for s in c.steps]
    assert {"Gains", "Set-off", "Exemption", "Tax", "Total"} <= set(sections)
    assert c.steps[-1]["amount"] == c.total


def test_buckets():
    assert te.bucket_for("non_equity", "ST") == "st_slab"  # gold ETF short term -> slab
    assert te.bucket_for("non_equity", "LT") == "lt_other"
    assert te.bucket_for("debt", "LT") == "st_slab"  # debt funds: always slab
    assert te.bucket_for("equity", "LT") == "lt_equity"


def test_long_term_is_strictly_more_than_12_months():
    l = lot("1", "A", dt.date(2025, 10, 12), 1, 100, 110)
    assert l.term_on(dt.date(2026, 10, 12)) == "ST" and l.term_on(dt.date(2026, 10, 13)) == "LT"


def test_fifo_legs_and_stt_not_deductible():
    lots = [lot("1", "A", dt.date(2024, 1, 1), 10, 100, 200), lot("2", "A", dt.date(2026, 6, 1), 10, 150, 200)]
    legs = te.make_legs(lots, 15, TODAY, SellCosts(expense_pct=0.01, dp_charge=0))
    assert [(l.key, l.units, l.term) for l in legs] == [("1", 10, "LT"), ("2", 5, "ST")]
    first = legs[0]
    assert first.stt == pytest.approx(2.0)  # 0.1% of 2000, not deducted from the gain
    assert first.gain == pytest.approx(2000 - 1000 - 0.2)
    res = te.evaluate(legs, Gains(), R)
    assert res.net == pytest.approx(res.proceeds - res.expenses - res.stt - res.tax)
    assert res.tax == pytest.approx((5 * 50 - 0.1) * 0.20 * 1.04, abs=0.01)  # LT part is within the exemption


def test_sale_tax_is_incremental_over_realised_gains():
    lots = [lot("1", "A", dt.date(2024, 1, 1), 100, 100, 200)]
    legs = te.make_legs(lots, 100, TODAY, SellCosts(expense_pct=0, dp_charge=0))
    fresh = te.evaluate(legs, Gains(), R)
    assert fresh.tax == 0  # 10,000 LTCG fully exempt
    used = te.evaluate(legs, Gains(lt_equity=125000), R)  # exemption already used this FY
    assert used.tax == pytest.approx(10000 * 0.125 * 1.04)


def test_raise_cash_prefers_tax_free_units_and_hits_target():
    holdings = {
        "LT": [lot("1", "LT", dt.date(2024, 1, 1), 100, 100, 150)],
        "ST": [lot("2", "ST", dt.date(2026, 9, 1), 100, 100, 150)],
    }
    plan = te.raise_cash(holdings, 3000, TODAY, Gains(), R, SellCosts(expense_pct=0, dp_charge=0))
    assert [h["name"] for h in plan["holdings"]] == ["LT"]
    assert plan["sale"]["tax"] == 0 and plan["achieved_net"] >= 3000
    # minimal whole units: 20 x 150 = 3000 leaves 2997 after 0.1% STT, so 21 are needed
    assert plan["holdings"][0]["units"] == 21


def test_raise_cash_uses_st_units_once_lt_exhausted():
    holdings = {"LT": [lot("1", "LT", dt.date(2024, 1, 1), 10, 100, 150)],
                "ST": [lot("2", "ST", dt.date(2026, 9, 1), 100, 100, 150)]}
    plan = te.raise_cash(holdings, 5000, TODAY, Gains(), R, SellCosts(expense_pct=0, dp_charge=0))
    assert {h["name"] for h in plan["holdings"]} == {"LT", "ST"} and plan["shortfall"] == 0
    assert plan["sale"]["tax"] > 0


def test_withdrawal_plan_resets_exemption_and_detects_depletion():
    holdings = {"A": [lot("1", "A", dt.date(2020, 1, 1), 1000, 100, 200)]}  # value 2L, all LT
    no_costs = SellCosts(expense_pct=0, dp_charge=0, stt_share_pct=0)
    out = te.withdrawal_plan(holdings, 10000, 30, 0.0, dt.date(2026, 11, 1), Gains(), R, no_costs)
    assert out["total_tax"] == 0  # gains per FY stay well under 1.25L
    assert out["months_funded"] == 20 and out["depleted_on"] is not None  # 2L / 10k


def test_wait_for_lt():
    lots = [lot("1", "A", dt.date(2026, 3, 1), 10, 100, 110), lot("2", "A", dt.date(2026, 5, 1), 10, 100, 110)]
    assert te.wait_for_lt(lots, 5, TODAY) == dt.date(2027, 3, 2)
    assert te.wait_for_lt(lots, 15, TODAY) == dt.date(2027, 5, 2)


def test_partial_sales_keep_cost_per_unit():
    l = lot("1", "A", dt.date(2024, 1, 1), 100, 100, 100)
    l.consume(30)
    assert l.units == 70 and l.unit_cost == pytest.approx(100)


def test_withdrawals_at_cost_price_book_no_phantom_losses():
    """Price == cost and 0% return: every month's gain must be ~0 (only charges)."""
    holdings = {"A": [lot("1", "A", dt.date(2020, 1, 1), 500, 100, 100)],
                "B": [lot("2", "B", dt.date(2021, 1, 1), 500, 50, 50)]}
    no_costs = SellCosts(expense_pct=0, dp_charge=0, stt_share_pct=0)
    out = te.withdrawal_plan(holdings, 3000, 20, 0.0, dt.date(2026, 11, 1), Gains(), R, no_costs)
    assert all(abs(r["gain"]) < 0.01 for r in out["rows"])
    assert out["total_net"] == pytest.approx(3000 * out["months_funded"], abs=1)


def test_raise_cash_repeated_partial_sales_of_one_lot():
    holdings = {"A": [lot("1", "A", dt.date(2024, 1, 1), 100, 100, 150)]}
    plan = te.raise_cash(holdings, 7000, TODAY, Gains(), R, SellCosts(expense_pct=0, dp_charge=0, stt_share_pct=0))
    h = plan["holdings"][0]
    assert h["gain"] == pytest.approx(h["units"] * 50)  # 50 gain per unit, regardless of chunking
