"""Cell-reference utilities and Excel-compatible formula reference shifting.

Everything in this module is pure (no I/O) so it can be unit tested in isolation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from openpyxl.formula.tokenizer import Tokenizer, Token
from openpyxl.formula.translate import Translator
from openpyxl.utils import column_index_from_string, get_column_letter

CELL_RE = re.compile(r"^(\$?)([A-Za-z]{1,3})(\$?)(\d+)$")
COL_RE = re.compile(r"^(\$?)([A-Za-z]{1,3})$")
ROW_RE = re.compile(r"^(\$?)(\d+)$")


def split_cell(ref: str) -> tuple[int, int]:
    """'B12' -> (12, 2). Accepts absolute markers."""
    m = CELL_RE.match(ref)
    if not m:
        raise ValueError(f"Not a cell reference: {ref}")
    return int(m.group(4)), column_index_from_string(m.group(2).upper())


def cell_name(row: int, col: int) -> str:
    return f"{get_column_letter(col)}{row}"


@dataclass(frozen=True)
class InsertSpec:
    """Describes a row insertion.

    at:     1-based row index; the new rows occupy [at, at+count).
    count:  number of rows inserted.

    Excel only grows a range when rows are inserted strictly inside it. When we append a
    row to the *bottom* of a block, aggregates such as ``=SUM(G3:G28)`` sitting right below
    the block would not include the new row. A human would fix this by inserting above the
    last row; we model the equivalent result with two narrowly-scoped extension rules that
    only apply to multi-row ranges whose last row is exactly ``at - 1``:

    extend_below: extend ranges in formulas located at/after the insertion point (i.e.
        aggregates below the block: sub totals, grand totals, summaries) and in
        conditional-format / validation / autofilter areas.
    group: (first,last) rows of an existing lot group being appended to. Formulas located
        inside the group whose range starts within the group (e.g. a per-stock
        ``=SUM(P20:P22)/SUM(I20:I22)``) are extended as well.
    """

    at: int
    count: int = 1
    extend_below: bool = False
    group: tuple[int, int] | None = None


def _shift_row(row: int, spec: InsertSpec) -> int:
    return row + spec.count if row >= spec.at else row


def _should_extend(lo: int, hi: int, spec: InsertSpec, formula_row: int | None) -> bool:
    if lo == hi or hi != spec.at - 1:
        return False
    if spec.extend_below and (formula_row is None or formula_row >= spec.at):
        return True
    if spec.group and formula_row is not None:
        g1, g2 = spec.group
        if g1 <= formula_row <= g2 and lo >= g1:
            return True
    return False


def _shift_span(r1: int, r2: int, spec: InsertSpec, formula_row: int | None = -1) -> tuple[int, int]:
    """Shift a (start,end) row span. ``formula_row`` = original row of the formula holding
    the reference; ``None`` means a non-formula area (CF/DV/autofilter); ``-1`` disables
    extension entirely (merged cells, defined names)."""
    lo, hi = min(r1, r2), max(r1, r2)
    new_lo = _shift_row(lo, spec)
    new_hi = _shift_row(hi, spec)
    if formula_row != -1 and _should_extend(lo, hi, spec, formula_row):
        new_hi = hi + spec.count
    return (new_lo, new_hi) if r1 <= r2 else (new_hi, new_lo)


def _fmt_cell(dc: str, col: str, dr: str, row: int) -> str:
    return f"{dc}{col}{dr}{row}"


def shift_area(area: str, spec: InsertSpec, formula_row: int | None = -1) -> str:
    """Shift an area reference without sheet prefix: 'A1', '$A$1:$B$9', 'A:A', '3:5'."""
    if ":" in area:
        a, b = area.split(":", 1)
        ma, mb = CELL_RE.match(a), CELL_RE.match(b)
        if ma and mb:
            r1, r2 = int(ma.group(4)), int(mb.group(4))
            n1, n2 = _shift_span(r1, r2, spec, formula_row)
            return (
                _fmt_cell(ma.group(1), ma.group(2), ma.group(3), n1)
                + ":"
                + _fmt_cell(mb.group(1), mb.group(2), mb.group(3), n2)
            )
        ra, rb = ROW_RE.match(a), ROW_RE.match(b)
        if ra and rb:
            n1, n2 = _shift_span(int(ra.group(2)), int(rb.group(2)), spec, formula_row)
            return f"{ra.group(1)}{n1}:{rb.group(1)}{n2}"
        return area  # whole-column ranges or unknown forms are row-independent
    m = CELL_RE.match(area)
    if m:
        return _fmt_cell(m.group(1), m.group(2), m.group(3), _shift_row(int(m.group(4)), spec))
    return area


def _split_sheet(operand: str) -> tuple[str | None, str]:
    if "!" in operand:
        sheet, area = operand.rsplit("!", 1)
        return sheet, area
    return None, operand


def _normalize_sheet_name(name: str) -> str:
    name = name.strip()
    if name.startswith("'") and name.endswith("'"):
        name = name[1:-1].replace("''", "'")
    return name


def shift_formula(
    formula: str, spec: InsertSpec, *, own_sheet: str, target_sheet: str, formula_row: int | None = -1
) -> str:
    """Shift references in ``formula`` (with or without leading '=') that point to
    ``target_sheet``. ``own_sheet`` is the sheet the formula lives in (unqualified refs
    resolve to it). ``formula_row`` is the original row of the formula cell when it lives
    in the target sheet (enables the extension rules of :class:`InsertSpec`)."""
    has_eq = formula.startswith("=")
    tok = Tokenizer(formula if has_eq else "=" + formula)
    changed = False
    for t in tok.items:
        if t.type == Token.OPERAND and t.subtype == Token.RANGE:
            sheet, area = _split_sheet(t.value)
            ref_sheet = _normalize_sheet_name(sheet) if sheet else own_sheet
            if ref_sheet != target_sheet:
                continue
            new_area = shift_area(area, spec, formula_row)
            if new_area != area:
                t.value = (sheet + "!" if sheet else "") + new_area
                changed = True
    if not changed:
        return formula
    rendered = tok.render()
    return rendered if has_eq else rendered[1:]


def shift_sqref(sqref: str, spec: InsertSpec, extendable: bool = False) -> str:
    """Shift a space separated list of areas (mergeCells, CF, DV, autoFilter...)."""
    return " ".join(shift_area(a, spec, None if extendable else -1) for a in sqref.split())


def translate(formula: str, origin: str, dest: str) -> str:
    """Translate a formula as Excel does when copy/pasting from origin to dest.
    Accepts formulas with or without '=' and returns without '='."""
    f = formula if formula.startswith("=") else "=" + formula
    return Translator(f, origin=origin).translate_formula(dest)[1:]


def canonical(formula: str, origin: str) -> str:
    """Relative-position-independent fingerprint of a formula (R1C1 equivalent):
    translate it to a fixed far-away anchor in the same column."""
    _, col = split_cell(origin)
    return translate(formula, origin, cell_name(100000, col))
