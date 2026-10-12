"""Indian capital-gains tax engine for listed securities (pure functions, no I/O).

Rules modelled (FY 2026-27, unchanged by Budget 2026; all rates configurable):

* Listed equity shares and equity-oriented fund units (STT paid):
    - short term (held <= 12 months): 20 %
    - long term  (held  > 12 months): 12.5 % on the part of the year's total LTCG above the
      Rs 1.25 lakh exemption
* Gold / silver / other non-equity listed ETFs: short term taxed at the slab rate,
  long term (> 12 months) 12.5 % without indexation and without the exemption.
* Debt funds bought after 1 Apr 2023: always slab rate.
* Set-off inside capital gains: short-term losses against any capital gain, long-term
  losses only against long-term gains. Losses are applied where they save the most tax.
  Unabsorbed losses carry forward (8 years) and are reported, not valued.
* Surcharge (user setting, 0/10/15 %; capped at 15 % for these gains) and 4 % health &
  education cess on top.
* STT is not deductible: it is excluded from the cost of acquisition and from sale
  expenses. Brokerage / exchange / SEBI / GST / DP charges on the sale are deductible.

Units are sold FIFO per security (the depository always debits the oldest units first),
so "which lot" is never a free choice: only "how many units of which security".

Not modelled (shown as assumptions in the UI): grandfathering for pre-2018 purchases,
carried-forward losses from earlier years, the basic-exemption-limit adjustment and rebates.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field, replace

from app.services.finance import add_months, fy_bounds, fy_label

EQUITY, NON_EQUITY, DEBT = "equity", "non_equity", "debt"
BUCKETS = ("st_equity", "lt_equity", "st_slab", "lt_other")
BUCKET_LABEL = {
    "st_equity": "Short-term gains on listed equity (held up to 12 months)",
    "lt_equity": "Long-term gains on listed equity (held over 12 months)",
    "st_slab": "Short-term gains taxed at your slab rate (gold/other ETFs, debt funds)",
    "lt_other": "Long-term gains on gold/other ETFs (held over 12 months)",
}
BUCKET_SHORT = {"st_equity": "Equity STCG", "lt_equity": "Equity LTCG", "st_slab": "STCG at slab", "lt_other": "Other LTCG"}


# =============================================================================== inputs
@dataclass(frozen=True)
class TaxRules:
    stcg_equity_pct: float = 20.0
    ltcg_pct: float = 12.5
    ltcg_exemption: float = 125000.0
    slab_pct: float = 30.0
    surcharge_pct: float = 0.0
    cess_pct: float = 4.0

    def rate(self, bucket: str) -> float:
        return {"st_equity": self.stcg_equity_pct, "lt_equity": self.ltcg_pct, "st_slab": self.slab_pct,
                "lt_other": self.ltcg_pct}[bucket] / 100.0

    @property
    def multiplier(self) -> float:
        """Base tax -> total tax after surcharge and cess."""
        return (1 + self.surcharge_pct / 100.0) * (1 + self.cess_pct / 100.0)


@dataclass(frozen=True)
class SellCosts:
    """Costs of selling. ``expense_pct`` = deductible charges (brokerage, exchange, SEBI,
    GST) as % of sale value; ``dp_charge`` = flat depository charge per security per sale."""

    expense_pct: float = 0.01
    dp_charge: float = 0.0
    stt_share_pct: float = 0.1  # delivery sale of equity shares
    stt_fund_pct: float = 0.001  # sale of equity-oriented fund / ETF units on exchange

    def stt_pct(self, kind: str) -> float:
        return {"share": self.stt_share_pct, "equity_fund": self.stt_fund_pct}.get(kind, 0.0)


@dataclass
class Gains:
    st_equity: float = 0.0
    lt_equity: float = 0.0
    st_slab: float = 0.0
    lt_other: float = 0.0

    def __add__(self, o: "Gains") -> "Gains":
        return Gains(*(getattr(self, b) + getattr(o, b) for b in BUCKETS))

    def add(self, bucket: str, amount: float) -> None:
        setattr(self, bucket, getattr(self, bucket) + amount)

    def as_dict(self) -> dict:
        return {b: round(getattr(self, b), 2) for b in BUCKETS}

    @property
    def total(self) -> float:
        return sum(getattr(self, b) for b in BUCKETS)


@dataclass
class TaxLot:
    """One purchase row as the tax engine sees it."""

    key: str  # stable id (sheet row)
    name: str
    date: dt.date
    units: float  # units held (bonus-adjusted)
    cost: float  # cost of acquisition for tax (sheet cost minus purchase STT)
    sheet_cost: float  # sheet's Total Purchase Price (incl. all charges)
    stt_buy: float
    price: float  # current price per unit
    tclass: str  # equity | non_equity | debt
    kind: str  # share | equity_fund | other (drives STT on sale)
    fractional: bool = False

    @property
    def unit_cost(self) -> float:
        return self.cost / self.units if self.units else 0.0

    def consume(self, units: float) -> None:
        """Remove sold units; cost, sheet cost and purchase STT shrink pro rata so the cost per
        remaining unit is unchanged."""
        if self.units <= 0:
            return
        keep = max(0.0, (self.units - units) / self.units)
        self.cost *= keep
        self.sheet_cost *= keep
        self.stt_buy *= keep
        self.units = max(0.0, self.units - units)

    def lt_date(self) -> dt.date:
        return add_months(self.date, 12)

    def term_on(self, sale: dt.date) -> str:
        return "LT" if sale > self.lt_date() else "ST"


def bucket_for(tclass: str, term: str) -> str:
    if tclass == DEBT:
        return "st_slab"
    if tclass == NON_EQUITY:
        return "st_slab" if term == "ST" else "lt_other"
    return "st_equity" if term == "ST" else "lt_equity"


# ========================================================================== computation
@dataclass
class TaxComputation:
    gains: dict  # net gains per bucket before set-off
    after_setoff: dict
    exemption_available: float
    exemption_applied: float
    taxable: dict
    base_tax: dict
    base_total: float
    surcharge: float
    cess: float
    total: float
    carried_forward: dict
    steps: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _r(x: float) -> float:
    return round(x, 2) + 0.0  # + 0.0 turns -0.0 into 0.0


def compute_tax(g: Gains, rules: TaxRules) -> TaxComputation:
    """Tax on the year's capital gains ``g`` (all realised gains of the FY, incl. any
    hypothetical sale). Records every step for display."""
    steps: list[dict] = []

    def step(section, label, amount=None, formula=None):
        steps.append({"section": section, "label": label, "amount": None if amount is None else _r(amount),
                      "formula": formula})

    for b in BUCKETS:
        v = getattr(g, b)
        if abs(v) >= 0.005:
            step("Gains", BUCKET_LABEL[b], v)

    pos = {b: max(getattr(g, b), 0.0) for b in BUCKETS}
    st_loss = -min(g.st_equity, 0.0) - min(g.st_slab, 0.0)
    lt_loss = -min(g.lt_equity, 0.0) - min(g.lt_other, 0.0)

    def absorb(loss: float, kind: str, targets: list[str]) -> float:
        for t in targets:
            if loss <= 0.005:
                break
            if pos[t] > 0:
                use = min(loss, pos[t])
                pos[t] -= use
                loss -= use
                step("Set-off", f"{kind} losses of {_fmt(use)} set off against {BUCKET_SHORT[t]}", -use)
        return loss

    # short-term losses: wherever they save most (highest rate first, exempt LTCG last)
    st_targets = sorted(["st_slab", "st_equity"], key=lambda b: -rules.rate(b)) + ["lt_other", "lt_equity"]
    st_left = absorb(st_loss, "Short-term", st_targets)
    lt_left = absorb(lt_loss, "Long-term", ["lt_other", "lt_equity"])
    if st_loss or lt_loss:
        step("Set-off", "Net gains after set-off", sum(pos.values()))

    exemption = rules.ltcg_exemption
    applied = min(pos["lt_equity"], exemption)
    if pos["lt_equity"] > 0:
        step("Exemption", f"Equity LTCG exemption ({_fmt(exemption)} per financial year) applied", -applied,
             f"min({_fmt(pos['lt_equity'])}, {_fmt(exemption)})")
    taxable = dict(pos)
    taxable["lt_equity"] = pos["lt_equity"] - applied

    base = {}
    for b in BUCKETS:
        base[b] = taxable[b] * rules.rate(b)
        if taxable[b] > 0.005:
            step("Tax", f"{BUCKET_SHORT[b]}: {_fmt(taxable[b])} × {rules.rate(b) * 100:g}%", base[b])
    base_total = sum(base.values())
    surcharge = base_total * rules.surcharge_pct / 100.0
    cess = (base_total + surcharge) * rules.cess_pct / 100.0
    total = base_total + surcharge + cess
    if base_total > 0.005:
        if rules.surcharge_pct:
            step("Tax", f"Surcharge {rules.surcharge_pct:g}%", surcharge, f"{_fmt(base_total)} × {rules.surcharge_pct:g}%")
        step("Tax", f"Health & education cess {rules.cess_pct:g}%", cess,
             f"{_fmt(base_total + surcharge)} × {rules.cess_pct:g}%")
    step("Total", "Total capital-gains tax", total)
    if st_left > 0.005 or lt_left > 0.005:
        step("Carry forward", f"Unabsorbed losses carried forward (8 years): short-term {_fmt(st_left)}, "
                              f"long-term {_fmt(lt_left)}")
    return TaxComputation(
        gains=g.as_dict(), after_setoff={b: _r(v) for b, v in pos.items()}, exemption_available=exemption,
        exemption_applied=_r(applied), taxable={b: _r(v) for b, v in taxable.items()},
        base_tax={b: _r(v) for b, v in base.items()}, base_total=_r(base_total), surcharge=_r(surcharge),
        cess=_r(cess), total=_r(total), carried_forward={"st": _r(st_left), "lt": _r(lt_left)}, steps=steps,
    )


def tax_total(g: Gains, rules: TaxRules) -> float:
    return compute_tax(g, rules).total


def _fmt(x: float) -> str:
    """₹ with Indian digit grouping (used in step labels)."""
    neg = x < 0
    x = abs(round(x))
    s = str(int(x))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("−" if neg else "") + "₹" + s


# ================================================================================= sales
@dataclass
class Leg:
    key: str
    name: str
    date: dt.date
    units: float
    price: float
    proceeds: float
    cost: float
    expenses: float  # deductible
    stt: float  # not deductible
    gain: float
    term: str
    bucket: str
    lt_date: dt.date

    def as_dict(self) -> dict:
        d = asdict(self)
        d["date"] = self.date.isoformat()
        d["lt_date"] = self.lt_date.isoformat()
        for k in ("proceeds", "cost", "expenses", "stt", "gain"):
            d[k] = _r(d[k])
        return d


def make_legs(lots_fifo: list[TaxLot], units: float, sale_date: dt.date, costs: SellCosts,
              price: float | None = None, include_dp: bool = True) -> list[Leg]:
    """Legs for selling ``units`` of one security FIFO at ``price`` (default: lot price)."""
    legs: list[Leg] = []
    left = units
    for lot in lots_fifo:
        if left <= 1e-9:
            break
        u = min(lot.units, left)
        if u <= 1e-9:
            continue
        p = lot.price if price is None else price
        proceeds = u * p
        expenses = proceeds * costs.expense_pct / 100.0
        stt = proceeds * costs.stt_pct(lot.kind) / 100.0
        cost = lot.unit_cost * u
        term = lot.term_on(sale_date)
        legs.append(Leg(lot.key, lot.name, lot.date, u, p, proceeds, cost, expenses, stt, 0.0, term,
                        bucket_for(lot.tclass, term), lot.lt_date()))
        left -= u
    if legs and include_dp and costs.dp_charge:
        tot = sum(l.proceeds for l in legs) or 1.0
        for l in legs:
            l.expenses += costs.dp_charge * l.proceeds / tot
    for l in legs:
        l.gain = l.proceeds - l.cost - l.expenses
    return legs


def gains_of(legs: list[Leg]) -> Gains:
    g = Gains()
    for l in legs:
        g.add(l.bucket, l.gain)
    return g


@dataclass
class SaleResult:
    legs: list[Leg]
    proceeds: float
    expenses: float
    stt: float
    gains: Gains
    tax_before: float
    tax_after: float
    tax: float  # incremental tax caused by this sale
    net: float  # cash kept after charges and tax
    computation: TaxComputation

    def as_dict(self) -> dict:
        return {
            "legs": [l.as_dict() for l in self.legs],
            "units": sum(l.units for l in self.legs),
            "proceeds": _r(self.proceeds), "expenses": _r(self.expenses), "stt": _r(self.stt),
            "gain": _r(self.gains.total), "gains": self.gains.as_dict(),
            "tax_before": _r(self.tax_before), "tax_after": _r(self.tax_after), "tax": _r(self.tax),
            "net": _r(self.net),
            "effective_tax_pct": _r(self.tax / self.gains.total * 100) if self.gains.total > 0.5 else None,
            "computation": self.computation.as_dict(),
        }


def evaluate(legs: list[Leg], realised: Gains, rules: TaxRules) -> SaleResult:
    g = gains_of(legs)
    before = tax_total(realised, rules)
    comp = compute_tax(realised + g, rules)
    proceeds = sum(l.proceeds for l in legs)
    expenses = sum(l.expenses for l in legs)
    stt = sum(l.stt for l in legs)
    tax = comp.total - before
    return SaleResult(legs, proceeds, expenses, stt, g, before, comp.total, tax, proceeds - expenses - stt - tax, comp)


# ============================================================================ optimiser
def _units_grid(lot: TaxLot) -> float:
    return 0.001 if lot.fractional else 1.0


def raise_cash(holdings: dict[str, list[TaxLot]], target_net: float, sale_date: dt.date, realised: Gains,
               rules: TaxRules, costs: SellCosts, exclude: set[str] | None = None,
               price_factor: float = 1.0) -> dict:
    """Choose what to sell so that cash after charges and tax >= ``target_net`` with the least
    tax + charges. Greedy over FIFO fronts: repeatedly sell from the security whose next
    (oldest) lot costs the least tax + charges per rupee raised. Returns the plan and the
    remaining units per lot (for multi-step simulations)."""
    exclude = exclude or set()
    remaining = {name: [replace(l) for l in lots] for name, lots in holdings.items() if name not in exclude}
    for lots in remaining.values():
        for l in lots:
            l.price = l.price * price_factor
    state = Gains(**asdict(realised))
    picks: dict[str, list[Leg]] = {}
    touched: set[str] = set()
    need = target_net

    def front(name):
        for l in remaining[name]:
            if l.units > 1e-9 and l.price > 0:
                return l
        return None

    def cost_of(name, lot, units):
        legs = make_legs([lot], units, sale_date, costs, include_dp=False)
        if name not in touched and costs.dp_charge:
            legs[0].expenses += costs.dp_charge
            legs[0].gain -= costs.dp_charge
        g = gains_of(legs)
        inc = tax_total(state + g, rules) - tax_total(state, rules)
        proceeds = legs[0].proceeds
        net = proceeds - legs[0].expenses - legs[0].stt - inc
        return legs, net, proceeds - net

    guard = 0
    while need > 0.5 and guard < 10000:
        guard += 1
        best = None
        for name in remaining:
            lot = front(name)
            if lot is None:
                continue
            legs, net, drag = cost_of(name, lot, lot.units)
            ratio = drag / (legs[0].proceeds or 1.0)
            if best is None or ratio < best[0]:
                best = (ratio, name, lot, legs, net)
        if best is None:
            break
        _, name, lot, legs, net = best
        if net > need:
            step = _units_grid(lot)
            lo, hi = 1, max(1, int(round(lot.units / step)))
            while lo < hi:  # smallest unit count whose net covers the need
                mid = (lo + hi) // 2
                if cost_of(name, lot, mid * step)[1] >= need:
                    hi = mid
                else:
                    lo = mid + 1
            legs, net, _ = cost_of(name, lot, lo * step)
        lot.consume(legs[0].units)
        state = state + gains_of(legs)
        picks.setdefault(name, []).extend(legs)
        touched.add(name)
        need -= net
    all_legs = [l for legs in picks.values() for l in legs]
    res = evaluate(all_legs, realised, rules)
    per_holding = []
    for name, legs in picks.items():
        g = gains_of(legs)
        per_holding.append({
            "name": name, "units": sum(l.units for l in legs), "price": _r(legs[0].price),
            "proceeds": _r(sum(l.proceeds for l in legs)), "gain": _r(g.total), "gains": g.as_dict(),
            "terms": sorted({l.term for l in legs}),
        })
    return {
        "target_net": _r(target_net), "achieved_net": _r(res.net), "shortfall": _r(max(0.0, target_net - res.net)),
        "holdings": per_holding, "sale": res.as_dict(),
        "_remaining": remaining,  # internal: units left after the plan
    }


# ======================================================================== withdrawals
def withdrawal_plan(holdings: dict[str, list[TaxLot]], monthly_net: float, months: int, annual_return_pct: float,
                    start: dt.date, realised_current_fy: Gains, rules: TaxRules, costs: SellCosts,
                    exclude: set[str] | None = None) -> dict:
    """Simulate withdrawing ``monthly_net`` (cash in hand, after charges and tax) every month
    for ``months`` months. Prices grow at ``annual_return_pct``; the tax-cheapest units are
    sold each month (FIFO per security); lots age into long term over time; the equity LTCG
    exemption resets every 1 April."""
    today = dt.date.today()
    lots_now = {n: [replace(l) for l in ls] for n, ls in holdings.items()}
    fy_label_cur = fy_label(start)
    realised = Gains(**asdict(realised_current_fy)) if fy_label(today) == fy_label_cur else Gains()
    rows, fy_totals = [], {}
    depleted = None
    totals = {"net": 0.0, "tax": 0.0, "charges": 0.0, "proceeds": 0.0, "gains": 0.0}
    for m in range(months):
        d = add_months(start, m)
        if fy_label(d) != fy_label_cur:
            fy_label_cur = fy_label(d)
            realised = Gains()
        factor = (1 + annual_return_pct / 100.0) ** (max(0, (d - today).days) / 365.0)
        plan = raise_cash(lots_now, monthly_net, d, realised, rules, costs, exclude, price_factor=factor)
        sale = plan["sale"]
        remaining = plan.pop("_remaining")
        # carry what was sold forward (prices stay as of today; growth is re-applied by factor)
        for name, ls in remaining.items():
            left = {l.key: l.units for l in ls}
            for l in lots_now[name]:
                if l.key in left and left[l.key] < l.units:
                    l.consume(l.units - left[l.key])
        realised = realised + Gains(**sale["gains"])
        value_left = sum(l.units * l.price * factor for ls in lots_now.values() for l in ls)
        row = {
            "date": d.isoformat(), "fy": fy_label(d), "net": sale["net"], "proceeds": sale["proceeds"],
            "tax": sale["tax"], "charges": _r(sale["expenses"] + sale["stt"]), "gain": sale["gain"],
            "st_gain": _r(sale["gains"]["st_equity"] + sale["gains"]["st_slab"]),
            "lt_gain": _r(sale["gains"]["lt_equity"] + sale["gains"]["lt_other"]),
            "sold": [{"name": h["name"], "units": h["units"]} for h in plan["holdings"]],
            "value_left": _r(value_left), "shortfall": plan["shortfall"],
        }
        rows.append(row)
        f = fy_totals.setdefault(row["fy"], {"fy": row["fy"], "net": 0.0, "tax": 0.0, "gain": 0.0, "lt_gain": 0.0})
        for k in ("net", "tax", "gain", "lt_gain"):
            f[k] = _r(f[k] + row[k])
        for k, src in (("net", "net"), ("tax", "tax"), ("charges", "charges"), ("proceeds", "proceeds"), ("gains", "gain")):
            totals[k] += row[src]
        if plan["shortfall"] > 0.5:
            depleted = d.isoformat()
            break
    return {
        "rows": rows, "by_fy": list(fy_totals.values()),
        "months_funded": sum(1 for r in rows if r["shortfall"] <= 0.5), "months_requested": months,
        "depleted_on": depleted,
        "total_net": _r(totals["net"]), "total_tax": _r(totals["tax"]), "total_charges": _r(totals["charges"]),
        "total_proceeds": _r(totals["proceeds"]),
        "effective_tax_pct": _r(totals["tax"] / totals["proceeds"] * 100) if totals["proceeds"] else None,
        "value_left": rows[-1]["value_left"] if rows else None,
    }


def wait_for_lt(lots_fifo: list[TaxLot], units: float, today: dt.date) -> dt.date | None:
    """First date on which every unit of a FIFO sale of ``units`` would be long term."""
    left, last = units, None
    for lot in lots_fifo:
        if left <= 1e-9:
            break
        u = min(lot.units, left)
        if lot.term_on(today) == "ST":
            d = lot.lt_date() + dt.timedelta(days=1)
            last = d if last is None or d > last else last
        left -= u
    return last


def fy_window(today: dt.date) -> tuple[dt.date, dt.date]:
    return fy_bounds(today)


def is_whole(x: float) -> bool:
    return math.isclose(x, round(x), abs_tol=1e-9)
