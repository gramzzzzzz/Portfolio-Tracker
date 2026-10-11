"""Pure financial helpers (no I/O)."""
from __future__ import annotations

import datetime as dt
import math


def xirr(flows: list[tuple[dt.date, float]]) -> float | None:
    """Annualised money-weighted return. Negative amounts = investments."""
    flows = [(d, a) for d, a in flows if a and d]
    if len(flows) < 2 or not any(a > 0 for _, a in flows) or not any(a < 0 for _, a in flows):
        return None
    t0 = min(d for d, _ in flows)
    ts = [((d - t0).days / 365.0, a) for d, a in flows]
    if max(t for t, _ in ts) < 1 / 365:
        return None

    def npv(r):
        return sum(a / (1 + r) ** t for t, a in ts)

    def dnpv(r):
        return sum(-t * a / (1 + r) ** (t + 1) for t, a in ts)

    r = 0.1
    for _ in range(100):
        try:
            f, df = npv(r), dnpv(r)
        except (OverflowError, ZeroDivisionError):
            break
        if df == 0:
            break
        nr = r - f / df
        if nr <= -0.9999:
            nr = (r - 0.9999) / 2
        if abs(nr - r) < 1e-9:
            return nr if math.isfinite(nr) else None
        r = nr
    # bisection fallback
    lo, hi = -0.9999, 100.0
    try:
        flo, fhi = npv(lo), npv(hi)
    except (OverflowError, ZeroDivisionError):
        return None
    if flo * fhi > 0:
        return None
    for _ in range(300):
        mid = (lo + hi) / 2
        fm = npv(mid)
        if abs(fm) < 1e-7:
            return mid
        if flo * fm < 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2


def fy_bounds(today: dt.date) -> tuple[dt.date, dt.date]:
    """Indian financial year (1 Apr - 31 Mar) containing ``today``."""
    start_year = today.year if today.month >= 4 else today.year - 1
    return dt.date(start_year, 4, 1), dt.date(start_year + 1, 3, 31)


def fy_label(d: dt.date) -> str:
    s, e = fy_bounds(d)
    return f"FY{s.year % 100:02d}-{e.year % 100:02d}"


def add_months(d: dt.date, months: int) -> dt.date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    last = [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return dt.date(y, m, min(d.day, last))
