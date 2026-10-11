import datetime as dt

import pytest

from app.dao.base import PasswordError, ParseError
from app.dao.contract_notes.registry import ContractNoteReader
from app.domain.models import Side


def test_parse_sample(note_bytes):
    n = ContractNoteReader().read(note_bytes, None)
    assert n.note_no == "262707234465"
    assert n.trade_date == dt.date(2026, 10, 1)
    assert n.settlement_date == dt.date(2026, 10, 5)
    assert n.net_payable == pytest.approx(5371.21)
    assert n.charges.stt == 5.0 and n.charges.stamp_duty == 1.0
    assert [(t.isin, t.side, t.qty, t.rate) for t in n.trades] == [
        ("INE154A01025", Side.BUY, 10, 260.0), ("INE155A01022", Side.BUY, 10, 276.5)]
    itc = n.trades[0]
    assert itc.charges == pytest.approx(3.01) and itc.net_value == pytest.approx(2603.01)
    assert n.trades[1].security_name == "TATA MOTORS PASS VEH LTD"
    assert itc.fills[0].trade_no == "02242797" and itc.fills[0].exchange == "NSEM"
    assert sum(t.net_value for t in n.trades) == pytest.approx(n.net_payable)
    assert not n.warnings


def test_encrypted_requires_password(encrypted_note):
    r = ContractNoteReader()
    with pytest.raises(PasswordError):
        r.read(encrypted_note, None)
    with pytest.raises(PasswordError):
        r.read(encrypted_note, "wrong")
    assert r.read(encrypted_note, "EHJPG1234").note_no == "262707234465"


def test_not_a_pdf():
    with pytest.raises(ParseError):
        ContractNoteReader().read(b"hello", None)
