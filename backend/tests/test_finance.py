import datetime as dt

from app.services.finance import add_months, fy_bounds, xirr


def test_xirr_one_year_ten_percent():
    r = xirr([(dt.date(2024, 1, 1), -1000), (dt.date(2024, 12, 31), 1100)])
    assert abs(r - 0.1003) < 0.001


def test_xirr_none_when_no_sign_change():
    assert xirr([(dt.date(2024, 1, 1), -1000)]) is None


def test_fy():
    assert fy_bounds(dt.date(2026, 10, 9)) == (dt.date(2026, 4, 1), dt.date(2027, 3, 31))
    assert fy_bounds(dt.date(2026, 2, 1)) == (dt.date(2025, 4, 1), dt.date(2026, 3, 31))
    assert add_months(dt.date(2024, 2, 29), 12) == dt.date(2025, 2, 28)
