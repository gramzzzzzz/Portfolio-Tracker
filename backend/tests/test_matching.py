import datetime as dt

from app.domain.models import Lot, Side, Trade
from app.services.matching import classify_trades, name_similarity, resolve_security

D = dt.date(2026, 10, 1)


def lot(row, name, qty, rate, date=D):
    return Lot(row, name, None, date, None, qty, rate, 3.0, qty * rate + 3, qty, rate, "FMCG", 0)


def trade(qty=10, rate=260.0, side=Side.BUY):
    return Trade("INE154A01025", "ITC LTD", side, qty, rate, qty * rate, stt=2.42, other_levies=0.59,
                 net_value=qty * rate + 3.01, trade_date=D)


def test_name_similarity():
    assert name_similarity("ITC LTD", "ITC") >= 0.85
    assert name_similarity("ITC LTD", "Infosys") < 0.5


def test_resolution_prefers_alias():
    r = resolve_security(trade(), ["ITC"], {"sheet_name": "ITC"}, [])
    assert r.sheet_name == "ITC" and r.confidence == 1.0
    r = resolve_security(trade(), ["ITC", "INFY"], None, [])
    assert r.sheet_name == "ITC"


def test_manual_row_is_duplicate():
    res = classify_trades([("a", trade(), "ITC")], [lot(12, "ITC", 10, 260.0)])
    assert res["a"].status == "duplicate" and res["a"].matched_rows == [12]


def test_manual_row_with_effective_rate_is_duplicate():
    # user typed the after-charges rate instead of the trade price
    res = classify_trades([("a", trade(), "ITC")], [lot(12, "ITC", 10, 260.30)])
    assert res["a"].status == "duplicate"


def test_possible_duplicate_and_one_row_one_trade():
    lots = [lot(12, "ITC", 10, 250.0)]
    res = classify_trades([("a", trade(), "ITC"), ("b", trade(), "ITC")], lots)
    assert res["a"].status == "possible_duplicate"
    assert res["b"].status == "new"  # the single sheet row can only absorb one trade
    res = classify_trades([("a", trade(), "ITC"), ("b", trade(), "ITC")], [lot(12, "ITC", 10, 260.0)])
    assert res["a"].status == "duplicate" and res["b"].status == "new"


def test_sells_are_left_to_classify_sells():
    assert classify_trades([("a", trade(side=Side.SELL), "ITC")], []) == {}


def test_sell_classification_against_transactions_sheet():
    from app.domain.models import Transaction, TxnType
    from app.services.matching import classify_sells

    s = trade(side=Side.SELL)  # 10 @ 260 on D
    rec = [Transaction("T-1", D, TxnType.SELL, "ITC", 10, 260.0)]
    assert classify_sells([("a", s, "ITC")], rec, {"itc": 30})["a"].status == "duplicate"
    assert classify_sells([("a", s, "ITC")], [], {"itc": 30})["a"].status == "sell"
    assert classify_sells([("a", s, "ITC")], [], {"itc": 5})["a"].status == "sell_exceeds"
    assert classify_sells([("a", s, None)], [], {})["a"].status == "sell_unknown"
    # two identical sells, one recorded: one duplicate + one new (each row absorbs one trade)
    two = classify_sells([("a", s, "ITC"), ("b", s, "ITC")], rec, {"itc": 30})
    assert (two["a"].status, two["b"].status) == ("duplicate", "sell")
    # available units are consumed across the batch
    more = classify_sells([("a", s, "ITC"), ("b", s, "ITC")], [], {"itc": 15})
    assert (more["a"].status, more["b"].status) == ("sell", "sell_exceeds")


def test_other_date_is_new():
    res = classify_trades([("a", trade(), "ITC")], [lot(12, "ITC", 10, 260.0, dt.date(2026, 9, 1))])
    assert res["a"].status == "new"
