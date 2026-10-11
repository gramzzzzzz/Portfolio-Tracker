from app.dao.xlsx import refs
from app.dao.xlsx.refs import InsertSpec


def shift(f, spec, row=-1):
    return refs.shift_formula(f, spec, own_sheet="S", target_sheet="S", formula_row=row)


def test_plain_shift_like_excel():
    s = InsertSpec(at=10)
    assert shift("=A9+A10+$B$12", s) == "=A9+A11+$B$13"
    assert shift("=SUM(A3:A28)", s) == "=SUM(A3:A29)"  # inside -> grows
    assert shift("=SUM(A10:A12)", s) == "=SUM(A11:A13)"  # starts at insertion -> moves
    assert shift("=SUM(A3:A9)", s) == "=SUM(A3:A9)"  # ends above -> untouched


def test_other_sheet_refs_not_touched():
    s = InsertSpec(at=5)
    assert refs.shift_formula("=Other!A9+A9", s, own_sheet="S", target_sheet="S") == "=Other!A9+A10"
    assert refs.shift_formula("=S!A9+A9", s, own_sheet="Other", target_sheet="S") == "=S!A10+A9"
    assert refs.shift_formula("='S'!A9", s, own_sheet="X", target_sheet="S") == "='S'!A10"


def test_boundary_extension_only_for_aggregates_below():
    s = InsertSpec(at=29, extend_below=True)
    assert shift("=SUM(G3:G28)", s, row=29) == "=SUM(G3:G29)"  # subtotal below the block
    assert shift("=SUM(P20:P28)", s, row=28) == "=SUM(P20:P28)"  # formula inside block untouched
    assert shift("=G28", s, row=29) == "=G28"  # single refs never extend


def test_group_extension():
    s = InsertSpec(at=23, group=(20, 22))
    assert shift("=SUM(P20:P22)/SUM(I20:I22)", s, row=22) == "=SUM(P20:P23)/SUM(I20:I23)"
    assert shift("=SUM(P19:P22)", s, row=22) == "=SUM(P19:P22)"  # starts outside the group


def test_translate_and_canonical():
    assert refs.translate("E4*F4", "G4", "G10") == "E10*F10"
    assert refs.canonical("C45/$C$59", "E45") == refs.canonical("C50/$C$59", "E50")
