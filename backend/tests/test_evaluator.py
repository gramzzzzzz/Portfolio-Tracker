from app.dao.xlsx.evaluator import XLError
from app.dao.xlsx.workbook import Workbook


def test_evaluator_matches_excel_cached_values(sheet_bytes):
    sh = Workbook(sheet_bytes).sheet("Shares")
    ev = sh.evaluator()
    n = 0
    for (r, c), cell in sh.cells().items():
        if cell.formula is None:
            continue
        n += 1
        got, exp = ev.value(r, c), cell.value
        if isinstance(exp, XLError):
            assert isinstance(got, XLError) and str(got) == str(exp), cell.coord
        else:
            assert abs(float(got) - float(exp)) < 1e-6, (cell.coord, got, exp)
    assert n > 300
