"""DAO for the 'Invt.xlsx' style portfolio tracking sheet.

This is the ONLY module that knows how the tracking sheet is laid out. Layout is discovered
at runtime from header labels and row classification (configurable via
:class:`InvtLayoutConfig`), so moving columns or adding blocks/sectors in Excel does not
require code changes. If the sheet's design changes fundamentally, write a new DAO that
implements :class:`app.dao.base.PortfolioSheetDAO`.

Write policy (enforced here):
  * the only mutation is inserting whole new rows;
  * new rows get input values only in input columns; every computed column receives the
    formula pattern already used by the surrounding rows of the same section;
  * existing cells are never modified (only moved down by the insertion, with references
    rewritten exactly like Excel does);
  * after writing, an independent verifier checks that every pre-existing cell is intact
    and that totals moved by exactly the amounts added. Any failure aborts the write.
"""
from __future__ import annotations

import datetime as dt
import io
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from app.dao.base import LayoutError
from app.domain.models import (
    AppendResult,
    Block,
    Issue,
    Lot,
    NewLot,
    PlannedRow,
    PriceUpdateResult,
    Section,
    SectorEntry,
    SheetSnapshot,
    SummaryEntry,
)

from . import refs
from .evaluator import BLANK, Unsupported, XLError
from .workbook import Cell, Sheet, Workbook, from_serial, to_serial


def norm(s) -> str:
    return re.sub(r"[^a-z0-9%]", "", str(s).lower()) if s is not None else ""


def name_key(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).casefold()


@dataclass
class InvtLayoutConfig:
    sheet_name: str | None = None
    # logical column -> (group label or None, label). Labels are matched after `norm`.
    main_columns: dict[str, tuple[str | None, tuple[str, ...]]] = field(
        default_factory=lambda: {
            "name": (None, ("Name",)),
            "code": (None, ("Code",)),
            "date": (None, ("Date",)),
            "face_value": (None, ("Face Value",)),
            "qty": ("Purchase Value", ("Qty",)),
            "rate": ("Purchase Value", ("Rate",)),
            "value": ("Purchase Value", ("Value",)),
            "charges": ("Purchase Value", ("Comn.", "Commission", "Charges")),
            "cost": ("Purchase Value", ("Total Purchase Price",)),
            "unit_cost": (None, ("Cost of One Unit",)),
            "held_qty": ("Market Value", ("Qty",)),
            "market_rate": ("Market Value", ("Rate",)),
            "market_value": ("Market Value", ("Value",)),
            "sell_charges": ("Market Value", ("Comn.", "Commission", "Charges")),
            "sale_total": ("Market Value", ("Total Sale Price",)),
            "gain": (None, ("Gain / Loss",)),
            "return_pct": (None, ("Return p.a",)),
            "years": (None, ("No.of yrs",)),
            "annual_pct": (None, ("Profit %",)),
        }
    )
    required_main: tuple[str, ...] = ("name", "date", "qty", "rate", "charges", "cost", "held_qty", "market_rate")
    # columns the application may write literal values into (everything else: formulas only)
    input_columns: tuple[str, ...] = ("name", "code", "date", "face_value", "qty", "rate", "charges", "market_rate")
    subtotal_labels: tuple[str, ...] = ("Sub Total", "Subtotal")
    grand_total_labels: tuple[str, ...] = ("Grand Total",)
    summary_label: str = "Summary"
    summary_columns: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: {
            "units": ("Units",),
            "amount": ("Amt.", "Amount"),
            "sector": ("Sector",),
            "pct": ("%age",),
            "avg": ("Avg.Price", "Avg Price"),
        }
    )
    sector_columns: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: {
            "sector": ("Sector",),
            "amount": ("Amout Invested", "Amount Invested"),
            "pct": ("%Age",),
        }
    )
    sector_total_label: str = "Total"
    reconcile_tolerance: float = 0.5
    stale_days: int = 30
    # well-known sector -> block synonyms used only when the data gives no evidence
    block_synonyms: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: {"mf": ("mutualfund", "mutualfunds"), "bank": ("banks", "banking")}
    )


# ============================================================================ layout model
@dataclass
class _Layout:
    sheet: Sheet
    cells: dict[tuple[int, int], Cell]
    header_rows: tuple[int, int]
    col: dict[str, int]
    sections: list[Section]
    row_kind: dict[int, str]  # lot | header | blank | subtotal | grand_total
    grand_total_row: int | None
    summary_row: int
    sum_col: dict[str, int]  # includes 'name'
    sec_col: dict[str, int]
    summary_rows: list[int]
    sector_rows: list[int]
    sector_total_row: int | None
    summary_total_row: int | None
    valuation_cell: tuple[int, int] | None

    def v(self, r, c):
        cell = self.cells.get((r, c))
        return BLANK if cell is None else cell.value

    def f(self, r, c):
        cell = self.cells.get((r, c))
        return None if cell is None else cell.formula

    def text(self, r, c) -> str | None:
        val = self.v(r, c)
        if isinstance(val, str) and not isinstance(val, XLError) and val.strip():
            return val.strip()
        return None

    def blocks(self) -> list[Block]:
        return [b for s in self.sections for b in s.blocks]


def _label_matches(value, labels: tuple[str, ...]) -> bool:
    return isinstance(value, str) and norm(value) in {norm(x) for x in labels}


def discover(sheet: Sheet, cfg: InvtLayoutConfig) -> _Layout:
    cells = sheet.cells()

    def v(r, c):
        cell = cells.get((r, c))
        return BLANK if cell is None else cell.value

    def f(r, c):
        cell = cells.get((r, c))
        return None if cell is None else cell.formula

    max_row = sheet.max_row()
    max_col = sheet.max_col()

    # ---- header rows: find 'Name' label in the first 10 rows
    name_lbl = cfg.main_columns["name"][1]
    h1 = None
    name_col = None
    for r in range(1, 11):
        for c in range(1, max_col + 1):
            if _label_matches(v(r, c), name_lbl):
                h1, name_col = r, c
                break
        if h1:
            break
    if h1 is None:
        raise LayoutError("Could not find the 'Name' header in the first rows")
    h2 = h1 + 1

    # group labels from merged ranges in the header row
    group_of: dict[int, str] = {}
    vertical: set[int] = set()
    for ref in sheet.merged_ranges():
        a, b = ref.split(":")
        r1, c1 = refs.split_cell(a)
        r2, c2 = refs.split_cell(b)
        if r1 == h1 and r2 == h1 and c2 > c1:
            lbl = v(r1, c1)
            for c in range(c1, c2 + 1):
                group_of[c] = lbl if isinstance(lbl, str) else ""
        if r1 == h1 and r2 >= h2 and c1 == c2:
            vertical.add(c1)

    col: dict[str, int] = {}
    for key, (group, labels) in cfg.main_columns.items():
        for c in range(1, max_col + 1):
            if group is not None:
                if c in group_of and norm(group_of[c]) == norm(group) and _label_matches(v(h2, c), labels):
                    col[key] = c
                    break
            else:
                if c in group_of:
                    continue
                if _label_matches(v(h1, c), labels) or _label_matches(v(h2, c), labels):
                    col[key] = c
                    break
    missing = [k for k in cfg.required_main if k not in col]
    if missing:
        raise LayoutError(f"Tracking sheet is missing required columns: {', '.join(missing)}")

    # ---- summary header row
    summary_row = None
    for r in range(h2 + 1, max_row + 1):
        if _label_matches(v(r, name_col), (cfg.summary_label,)):
            summary_row = r
            break
    if summary_row is None:
        raise LayoutError("Could not find the 'Summary' block")

    # ---- classify main area rows
    row_kind: dict[int, str] = {}
    grand_total_row = None
    cname, cqty, crate, cval = col["name"], col["qty"], col["rate"], col.get("value", col["cost"])

    def is_num(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool)

    for r in range(h2 + 1, summary_row):
        a = v(r, cname)
        a_txt = a.strip() if isinstance(a, str) and not isinstance(a, XLError) else ""
        if _label_matches(a_txt, cfg.grand_total_labels):
            row_kind[r] = "grand_total"
            grand_total_row = r
            break
        fval = f(r, cval) or f(r, col["cost"]) or ""
        if _label_matches(a_txt, cfg.subtotal_labels) or (not a_txt and fval.upper().startswith("SUM(")):
            row_kind[r] = "subtotal"
        elif a_txt and (is_num(v(r, cqty)) or is_num(v(r, crate))):
            row_kind[r] = "lot"
        elif a_txt:
            row_kind[r] = "header"
        else:
            row_kind[r] = "blank"
    if grand_total_row is None:
        raise LayoutError("Could not find the 'Grand Total' row")

    # ---- sections & blocks
    sections: list[Section] = []
    start = h2 + 1
    for r in range(h2 + 1, grand_total_row):
        if row_kind[r] != "subtotal":
            continue
        blocks: list[Block] = []
        cur: Block | None = None
        for rr in range(start, r):
            k = row_kind[rr]
            if k == "header":
                if cur:
                    blocks.append(cur)
                cur = Block(header=v(rr, cname).strip(), header_row=rr, first_row=rr, last_row=rr, section=len(sections))
            else:
                if cur is None:
                    cur = Block(header=None, header_row=None, first_row=rr, last_row=rr, section=len(sections))
                cur.last_row = rr
        if cur:
            blocks.append(cur)
        title = blocks[0].header if blocks else None
        sections.append(Section(index=len(sections), first_row=start, subtotal_row=r, blocks=blocks, title=title))
        start = r + 1
    if not sections:
        raise LayoutError("No 'Sub Total' rows found in the main area")

    # ---- summary & sector tables
    sum_col = {"name": name_col}
    sec_col: dict[str, int] = {}
    sector_hits = [c for c in range(1, max_col + 1) if _label_matches(v(summary_row, c), cfg.summary_columns["sector"])]
    # the summary table spans from the 'Summary' label up to the second 'Sector' label (if any)
    sum_end = sector_hits[1] - 1 if len(sector_hits) > 1 else max_col
    if sector_hits:
        sum_col["sector"] = sector_hits[0]
    for key, labels in cfg.summary_columns.items():
        if key == "sector":
            continue
        for c in range(name_col, sum_end + 1):
            if _label_matches(v(summary_row, c), labels):
                sum_col[key] = c
                break
    if len(sector_hits) > 1:
        sec_col["sector"] = sector_hits[1]
        for key, labels in cfg.sector_columns.items():
            if key == "sector":
                continue
            for c in range(sector_hits[1] + 1, max_col + 1):
                if _label_matches(v(summary_row, c), labels):
                    sec_col[key] = c
                    break
    for k in ("units", "amount", "sector"):
        if k not in sum_col:
            raise LayoutError(f"Summary block is missing column '{k}'")

    summary_rows: list[int] = []
    r = summary_row + 1
    while r <= max_row:
        has = (
            isinstance(v(r, name_col), str)
            or isinstance(v(r, sum_col["sector"]), str)
            or f(r, sum_col["units"]) is not None
        )
        if not has:
            break
        summary_rows.append(r)
        r += 1
    summary_total_row = None
    amt_c = sum_col["amount"]
    for rr in range(r, min(max_row, r + 6) + 1):
        ff = f(rr, amt_c)
        if ff and ff.upper().startswith("SUM("):
            summary_total_row = rr
            break

    sector_rows: list[int] = []
    sector_total_row = None
    if sec_col:
        sc = sec_col["sector"]
        for rr in range(summary_row + 1, max_row + 1):
            val = v(rr, sc)
            if _label_matches(val, (cfg.sector_total_label,)):
                sector_total_row = rr
                break
            if isinstance(val, str) and val.strip():
                sector_rows.append(rr)
            elif sector_rows and rr > sector_rows[-1] + 3:
                break

    valuation_cell = (h1, col["years"]) if "years" in col else None
    return _Layout(
        sheet=sheet,
        cells=cells,
        header_rows=(h1, h2),
        col=col,
        sections=sections,
        row_kind=row_kind,
        grand_total_row=grand_total_row,
        summary_row=summary_row,
        sum_col=sum_col,
        sec_col=sec_col,
        summary_rows=summary_rows,
        sector_rows=sector_rows,
        sector_total_row=sector_total_row,
        summary_total_row=summary_total_row,
        valuation_cell=valuation_cell,
    )


# =============================================================================== the DAO
class InvtSheetDAO:
    """PortfolioSheetDAO implementation for the Invt.xlsx layout."""

    def __init__(self, config: InvtLayoutConfig | None = None):
        self.cfg = config or InvtLayoutConfig()

    # ------------------------------------------------------------------ helpers
    def _open(self, data: bytes) -> tuple[Workbook, Sheet]:
        try:
            wb = Workbook(data)
        except Exception as e:  # zipfile.BadZipFile, KeyError, XML errors
            raise LayoutError(f"Not a valid .xlsx workbook: {e}") from e
        name = self.cfg.sheet_name
        if name is None:
            for n in wb.sheet_names():
                try:
                    discover(wb.sheet(n), self.cfg)
                    name = n
                    break
                except LayoutError:
                    continue
        if name is None or name not in wb.sheet_paths:
            raise LayoutError("No worksheet matches the tracking sheet layout")
        return wb, wb.sheet(name)

    # --------------------------------------------------------------------- read
    def read(self, data: bytes) -> SheetSnapshot:
        _, sheet = self._open(data)
        lay = discover(sheet, self.cfg)
        return self._snapshot(lay)

    def _snapshot(self, lay: _Layout) -> SheetSnapshot:
        ev = lay.sheet.evaluator()

        def num(r, c):
            if c is None:
                return None
            try:
                x = ev.value(r, c)
            except Unsupported:
                x = lay.v(r, c)
            if isinstance(x, (int, float)) and not isinstance(x, bool):
                return float(x)
            return None

        col = lay.col
        issues: list[Issue] = []
        # valuation date
        valuation_date = None
        if lay.valuation_cell:
            s = num(*lay.valuation_cell)
            if s and s > 0:
                valuation_date = from_serial(s).date()

        summary: list[SummaryEntry] = []
        sc = lay.sum_col
        for r in lay.summary_rows:
            nm = lay.text(r, sc["name"])
            summary.append(
                SummaryEntry(
                    row=r,
                    name=nm or "",
                    sector=lay.text(r, sc["sector"]),
                    units=num(r, sc["units"]),
                    amount=num(r, sc["amount"]),
                )
            )
        sector_of = {name_key(s.name): s.sector for s in summary if s.name}

        sectors = [
            SectorEntry(
                row=r,
                name=lay.text(r, lay.sec_col["sector"]),
                amount=num(r, lay.sec_col.get("amount")),
                pct=num(r, lay.sec_col.get("pct")),
            )
            for r in lay.sector_rows
        ]

        lots: list[Lot] = []
        blank_rows: list[int] = []
        for sec in lay.sections:
            for blk in sec.blocks:
                for r in range(blk.first_row, blk.last_row + 1):
                    k = lay.row_kind[r]
                    if k == "blank":
                        blank_rows.append(r)
                    if k != "lot":
                        continue
                    nm = lay.text(r, col["name"])
                    dval = lay.cells.get((r, col["date"]))
                    date = None
                    if dval is not None and isinstance(dval.value, float) and dval.value > 0:
                        date = from_serial(dval.value).date()
                    elif dval is not None and isinstance(dval.value, str):
                        for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d-%b-%y", "%d-%b-%Y", "%Y-%m-%d"):
                            try:
                                date = dt.datetime.strptime(dval.value.strip(), fmt).date()
                                break
                            except ValueError:
                                pass
                    qty = num(r, col["qty"]) or 0.0
                    rate = num(r, col["rate"]) or 0.0
                    charges = num(r, col["charges"]) or 0.0
                    cost = num(r, col["cost"])
                    if cost is None:
                        cost = qty * rate + charges
                    held = num(r, col["held_qty"])
                    lots.append(
                        Lot(
                            row=r,
                            name=nm,
                            code=(str(lay.v(r, col["code"])).strip() if "code" in col and lay.v(r, col["code"]) is not BLANK else None),
                            date=date,
                            face_value=num(r, col.get("face_value")),
                            qty=qty,
                            rate=rate,
                            charges=charges,
                            cost=cost,
                            held_qty=held if held is not None else qty,
                            market_rate=num(r, col["market_rate"]),
                            block=blk.header,
                            section=sec.index,
                            sector=sector_of.get(name_key(nm)),
                        )
                    )

        sector_to_block = self._sector_block_map(lay, lots, summary, sectors)
        block_to_sector = {}
        for s, b in sector_to_block.items():
            block_to_sector.setdefault(b, s)
        for lot in lots:
            if lot.sector is None and lot.block in block_to_sector:
                lot.sector = block_to_sector[lot.block]

        # ---- totals
        gt = lay.grand_total_row
        totals = {
            "grand_total_cost": num(gt, col["cost"]),
            "grand_total_value": num(gt, col.get("sale_total") or col.get("market_value")),
            "grand_total_gain": num(gt, col.get("gain")),
            "summary_total": num(lay.summary_total_row, sc["amount"]) if lay.summary_total_row else None,
            "sector_total": num(lay.sector_total_row, lay.sec_col.get("amount")) if lay.sector_total_row else None,
            "lots_cost": round(sum(l.cost for l in lots), 2),
        }

        # ---- data quality issues
        tol = self.cfg.reconcile_tolerance
        if totals["summary_total"] is not None and totals["grand_total_cost"] is not None:
            diff = totals["summary_total"] - totals["grand_total_cost"]
            if abs(diff) > tol:
                issues.append(Issue("warning", "summary_mismatch",
                    f"Summary 'Amt.' total ({totals['summary_total']:,.2f}) differs from the Grand Total purchase "
                    f"price ({totals['grand_total_cost']:,.2f}) by {diff:,.2f}. Some holdings are missing from "
                    "the summary or are named differently there.", [lay.summary_total_row]))
        if totals["sector_total"] is not None and totals["summary_total"] is not None:
            diff = totals["sector_total"] - totals["summary_total"]
            if abs(diff) > tol:
                issues.append(Issue("warning", "sector_mismatch",
                    f"Sector table total ({totals['sector_total']:,.2f}) differs from the summary total by "
                    f"{diff:,.2f}. A sector used in the summary is missing from the sector table.",
                    [lay.sector_total_row]))
        summary_keys = {name_key(s.name) for s in summary if s.name}
        missing = sorted({l.name for l in lots if name_key(l.name) not in summary_keys})
        for nm in missing:
            issues.append(Issue("warning", "lot_not_in_summary",
                f"'{nm}' has purchase rows but no Summary row, so it is excluded from sector allocation.",
                [l.row for l in lots if l.name == nm]))
        lot_keys = {name_key(l.name) for l in lots}
        for s in summary:
            if s.name and name_key(s.name) not in lot_keys:
                issues.append(Issue("info", "summary_without_lots",
                    f"Summary row '{s.name}' has no matching purchase rows (name mismatch?).", [s.row]))
        sector_names = {name_key(s.name) for s in sectors}
        for s in summary:
            if s.sector and sector_names and name_key(s.sector) not in sector_names:
                issues.append(Issue("warning", "sector_not_in_table",
                    f"Sector '{s.sector}' (used by '{s.name or 'row ' + str(s.row)}') is not listed in the sector table.",
                    [s.row]))
        no_rate = [l.row for l in lots if not l.market_rate]
        if no_rate:
            issues.append(Issue("info", "missing_market_rate", "Some purchase rows have no market rate.", no_rate))
        no_date = [l.row for l in lots if l.date is None]
        if no_date:
            issues.append(Issue("warning", "missing_date", "Some purchase rows have no purchase date.", no_date))
        if valuation_date and (dt.date.today() - valuation_date).days > self.cfg.stale_days:
            issues.append(Issue("info", "stale_valuation",
                f"Market rates in the sheet are as of {valuation_date:%d %b %Y} "
                f"({(dt.date.today() - valuation_date).days} days old)."))
        err_rows = []
        for l in lots:
            for key in ("cost", "unit_cost", "gain"):
                c = col.get(key)
                if c is not None:
                    try:
                        if isinstance(ev.value(l.row, c), XLError):
                            err_rows.append(l.row)
                            break
                    except Unsupported:
                        pass
        if err_rows:
            issues.append(Issue("warning", "formula_errors", "Formula errors in purchase rows.", err_rows))

        return SheetSnapshot(
            sheet_name=lay.sheet.name,
            valuation_date=valuation_date,
            lots=lots,
            summary=summary,
            sectors=sectors,
            sections=lay.sections,
            totals=totals,
            issues=issues,
            blank_rows=blank_rows,
            sector_to_block=sector_to_block,
        )

    def _sector_block_map(self, lay, lots, summary, sectors) -> dict[str, str]:
        votes: dict[str, Counter] = defaultdict(Counter)
        sector_of = {name_key(s.name): s.sector for s in summary if s.name and s.sector}
        for l in lots:
            s = sector_of.get(name_key(l.name))
            if s and l.block:
                votes[s][l.block] += 1
        out = {s: c.most_common(1)[0][0] for s, c in votes.items()}
        headers = [b.header for b in lay.blocks() if b.header]
        all_sectors = {s.name for s in sectors if s.name} | {s.sector for s in summary if s.sector}
        for s in sorted(all_sectors):
            if s in out:
                continue
            ns = norm(s)
            cand = None
            for h in headers:
                nh = norm(h)
                if nh == ns or nh.rstrip("s") == ns.rstrip("s") or nh in self.cfg.block_synonyms.get(ns, ()):
                    cand = h
                    break
            if cand is None:
                for h in headers:
                    nh = norm(h)
                    if len(nh) >= 3 and (nh in ns or ns in nh):
                        cand = h
                        break
            if cand:
                out[s] = cand
        return out

    # ------------------------------------------------------------- price refresh
    def _valuation_input(self, lay: _Layout) -> tuple[int, int] | None:
        """The cell the user types the valuation date into. In Invt.xlsx 'No.of yrs' header
        S1 is ``=L3``, so the input cell is L3. Only a plain single-cell reference (or a
        literal date in S1 itself) is followed; anything else is left alone."""
        if not lay.valuation_cell:
            return None
        r, c = lay.valuation_cell
        f = lay.f(r, c)
        if f is None:
            return (r, c) if isinstance(lay.v(r, c), float) else None
        ref = f.strip().lstrip("=")
        if "!" in ref or not refs.CELL_RE.match(ref):
            return None
        rr, cc = refs.split_cell(ref)
        return (rr, cc) if lay.f(rr, cc) is None else None

    def update_prices(self, data: bytes, prices: dict[str, float], as_of: dt.date | None
                      ) -> tuple[bytes | None, PriceUpdateResult]:
        wb, sheet = self._open(data)
        lay = discover(sheet, self.cfg)
        before_cells = {k: (c.value, c.formula) for k, c in lay.cells.items()}
        before = self._snapshot(lay)
        col, cname = lay.col["market_rate"], lay.col["name"]
        by_key = {name_key(n): round(float(p), 2) for n, p in prices.items() if p and p > 0}
        targets: dict[tuple[int, int], Any] = {}
        updated, unchanged, skipped, seen = [], [], [], set()
        for r, kind in sorted(lay.row_kind.items()):
            if kind != "lot":
                continue
            key = name_key(lay.text(r, cname) or "")
            if key not in by_key:
                continue
            seen.add(key)
            cell = lay.cells.get((r, col))
            if cell is not None and cell.formula is not None:
                skipped.append(r)  # never overwrite a formula
                continue
            cur = cell.value if cell is not None else BLANK
            new = by_key[key]
            if isinstance(cur, float) and abs(cur - new) < 0.005:
                unchanged.append(r)
                continue
            targets[(r, col)] = new
            updated.append(r)
        missing = sorted({l.name for l in before.lots if name_key(l.name) not in seen})
        vcell = self._valuation_input(lay)
        vname = refs.cell_name(*vcell) if vcell else None
        if vcell and as_of:
            cur = lay.v(*vcell)
            if not (isinstance(cur, float) and int(cur) == int(to_serial(as_of))):
                targets[vcell] = as_of
        if not targets:
            return None, PriceUpdateResult(False, [], unchanged, skipped, missing, vname, as_of, [])
        for (r, c), v in targets.items():
            sheet.set_cell(r, c, v)  # keeps the cell's existing style
        wb.mark_full_calc()
        sheet.refresh_cached_values()
        out = wb.to_bytes()
        checks = self._verify_prices(out, before, before_cells, targets)
        failed = [c for c in checks if not c["passed"]]
        if failed:
            raise RuntimeError("Price update verification failed, sheet NOT modified: "
                               + "; ".join(c["detail"] for c in failed))
        return out, PriceUpdateResult(True, updated, unchanged, skipped, missing, vname, as_of, checks)

    def _verify_prices(self, new_bytes, before: SheetSnapshot, before_cells, targets) -> list[dict]:
        sh2 = Workbook(new_bytes).sheet(before.sheet_name)
        after_cells = sh2.cells()
        bad = []
        for (r, c), (val, f) in before_cells.items():
            if (r, c) in targets:
                continue
            cell = after_cells.get((r, c))
            if cell is None:
                if not (val is BLANK and f is None):
                    bad.append(f"{refs.cell_name(r, c)} missing")
                continue
            if f is not None:
                if cell.formula != f:
                    bad.append(f"{refs.cell_name(r, c)} formula changed")
            elif cell.formula is not None or (cell.value != val and not (val is BLANK and cell.value is BLANK)):
                bad.append(f"{refs.cell_name(r, c)} value changed")
        extra = [k for k in after_cells if k not in before_cells and k not in targets
                 and not (after_cells[k].value is BLANK and after_cells[k].formula is None)]
        checks = [{"name": "other_cells_intact", "passed": not bad and not extra,
                   "detail": "Only market-rate / valuation-date cells changed" if not bad and not extra
                   else "; ".join((bad + [f"new cell {refs.cell_name(*k)}" for k in extra])[:5])}]
        wrong = []
        for (r, c), v in targets.items():
            cell = after_cells.get((r, c))
            exp = float(int(to_serial(v))) if isinstance(v, dt.date) else float(v)
            if cell is None or cell.formula is not None or not isinstance(cell.value, float) or abs(cell.value - exp) > 1e-6:
                wrong.append(refs.cell_name(r, c))
        checks.append({"name": "targets_written", "passed": not wrong,
                       "detail": f"{len(targets)} cell(s) written" if not wrong else f"wrong value in {', '.join(wrong)}"})
        after = self._snapshot(discover(sh2, self.cfg))
        same_cost = abs((after.totals.get("grand_total_cost") or 0) - (before.totals.get("grand_total_cost") or 0)) < 0.005
        checks.append({"name": "cost_unchanged", "passed": same_cost and len(after.lots) == len(before.lots),
                       "detail": "Purchase rows and invested total unchanged"})
        return checks

    # ------------------------------------------------------------------- append
    def append(self, data: bytes, lots: list[NewLot], *, dry_run: bool = False) -> tuple[bytes | None, AppendResult]:
        wb, sheet = self._open(data)
        before_lay = discover(sheet, self.cfg)
        before = self._snapshot(before_lay)
        before_cells = {k: (c.value, c.formula) for k, c in before_lay.cells.items()}
        specs: list[refs.InsertSpec] = []
        planned: list[PlannedRow] = []
        warnings: list[str] = []

        for nl in lots:
            self._append_one(sheet, nl, specs, planned, warnings)

        # renumber planned rows for later insertions above them
        final_rows = []
        for i, p in enumerate(planned):
            r = p.row
            for spec in specs[p.values.pop("_spec_index") + 1:]:
                if r >= spec.at:
                    r += spec.count
            p.row = r
            final_rows.append(p)

        unevaluated = sheet.refresh_cached_values()
        if unevaluated:
            warnings.append(f"{len(unevaluated)} formula(s) could not be pre-computed; Excel will compute them on open.")
        new_bytes = wb.to_bytes()
        checks = self._verify(data, new_bytes, before, before_cells, specs, lots, final_rows)
        failed = [c for c in checks if not c["passed"]]
        if failed:
            raise RuntimeError("Verification failed, sheet NOT modified: " + "; ".join(c["detail"] for c in failed))
        return (None if dry_run else new_bytes), AppendResult(rows=final_rows, checks=checks, warnings=warnings)

    # The per-lot algorithm. Re-discovers layout after each insertion.
    def _append_one(self, sheet: Sheet, nl: NewLot, specs, planned, warnings):
        cfg = self.cfg
        lay = discover(sheet, cfg)
        col = lay.col
        key = name_key(nl.name)
        lot_rows = [r for r, k in lay.row_kind.items() if k == "lot" and name_key(lay.text(r, col["name"])) == key]
        lay_blocks = lay.blocks()

        def block_of(r):
            for b in lay_blocks:
                if b.first_row <= r <= b.last_row:
                    return b
            return None

        header_needed = None
        if lot_rows:
            last = max(lot_rows)
            first = last
            while first - 1 in lot_rows:
                first -= 1
            blk = block_of(last)
            at = last + 1
            spec = refs.InsertSpec(at=at, count=1, extend_below=(last == blk.last_row), group=(first, last))
            template = last
            section = blk.section
        else:
            target_header = nl.block or self._snapshot_sector_block(lay, nl.sector)
            blk = next((b for b in lay_blocks if b.header and name_key(b.header) == name_key(target_header or "")), None)
            if blk is not None:
                lots_in = [r for r in range(blk.first_row, blk.last_row + 1) if lay.row_kind[r] == "lot"]
                # after the last lot; for an empty block, after its last (blank) row so the new
                # row lands inside the aggregates, which usually start below the heading
                anchor = max(lots_in) if lots_in else blk.last_row
                at = anchor + 1
                spec = refs.InsertSpec(at=at, count=1, extend_below=(anchor == blk.last_row))
                template = anchor if lots_in else self._template_lot_row(lay, blk.section, blk)
                section = blk.section
            else:
                sec = max(lay.sections, key=lambda s: (len(s.blocks), -s.index))
                at = sec.subtotal_row
                header_needed = nl.block or nl.sector
                spec = refs.InsertSpec(at=at, count=2, extend_below=True)
                template = self._template_lot_row(lay, sec.index, None)
                section = sec.index

        header_tmpl = None
        if header_needed:
            hdrs = [b.header_row for b in lay.sections[section].blocks if b.header_row]
            header_tmpl = hdrs[-1] if hdrs else None
        tmpl_shift = lambda r: r + spec.count if (r is not None and r >= spec.at) else r  # noqa: E731

        sheet.insert_rows(spec)
        specs.append(spec)
        idx = len(specs) - 1
        # formula pattern of this section, taken AFTER the insertion so absolute references
        # already point at the shifted cells
        patterns = self._section_patterns(discover(sheet, cfg), section)
        lot_row = at
        if header_needed:
            sheet.create_row(at, tmpl_shift(header_tmpl))
            sheet.set_cell(at, col["name"], header_needed)
            planned.append(PlannedRow("block_header", at, f"New block heading '{header_needed}'",
                                      nl.ref, {"_spec_index": idx}))
            lot_row = at + 1
        sheet.create_row(lot_row, tmpl_shift(template))
        values = {
            "name": nl.name, "code": nl.code, "date": nl.date, "face_value": nl.face_value,
            "qty": nl.qty, "rate": nl.rate, "charges": round(nl.charges, 2), "market_rate": nl.market_rate,
        }
        for k in cfg.input_columns:
            if k in col and values.get(k) is not None and k not in patterns:
                sheet.set_cell(lot_row, col[k], values[k])
        for c, canon in patterns.items():
            sheet.set_cell(lot_row, c, formula=refs.translate(canon, refs.cell_name(100000, c), refs.cell_name(lot_row, c)))
        # Columns that are formulas elsewhere but typed by hand in this section (e.g. the
        # Mutual Fund block has no '=E*F' in 'Value'): fill them like a human would.
        gross = round(nl.qty * nl.rate, 2)
        fallback = {"value": gross, "cost": round(gross + round(nl.charges, 2), 2), "held_qty": nl.qty}
        for k, val in fallback.items():
            c = col.get(k)
            if c is not None and c not in patterns:
                sheet.set_cell(lot_row, c, val)
                values[k] = val
        planned.append(PlannedRow("lot", lot_row, f"Purchase row for '{nl.name}' ({nl.qty:g} @ {nl.rate:,.2f})",
                                  nl.ref, {"_spec_index": idx, **{k: (str(v) if isinstance(v, dt.date) else v) for k, v in values.items() if v is not None}}))

        # ---- summary / sector tables
        lay = discover(sheet, cfg)
        need_summary = key not in {name_key(lay.text(r, lay.sum_col["name"]) or "") for r in lay.summary_rows}
        need_sector = bool(lay.sec_col) and name_key(nl.sector) not in {
            name_key(lay.text(r, lay.sec_col["sector"]) or "") for r in lay.sector_rows
        }
        if need_sector:
            last = lay.sector_rows[-1]
            at = last + 1
            combined = need_summary and lay.summary_rows and lay.summary_rows[0] <= at <= lay.summary_rows[-1] + 1
            self._insert_table_row(sheet, lay, at, last, specs, planned, nl,
                                   summary=combined, sector=True)
            need_summary = need_summary and not combined
            lay = discover(sheet, cfg)
        if need_summary:
            last = lay.summary_rows[-1]
            self._insert_table_row(sheet, lay, last + 1, last, specs, planned, nl, summary=True, sector=False)

    def _snapshot_sector_block(self, lay: _Layout, sector: str) -> str | None:
        # lightweight version of the sector->block mapping (no evaluation needed)
        snap_lots = []
        col = lay.col
        for b in lay.blocks():
            for r in range(b.first_row, b.last_row + 1):
                if lay.row_kind[r] == "lot":
                    snap_lots.append(type("L", (), {"name": lay.text(r, col["name"]), "block": b.header}))
        summary = [SummaryEntry(r, lay.text(r, lay.sum_col["name"]) or "", lay.text(r, lay.sum_col["sector"]))
                   for r in lay.summary_rows]
        sectors = [SectorEntry(r, lay.text(r, lay.sec_col["sector"])) for r in lay.sector_rows] if lay.sec_col else []
        mapping = self._sector_block_map(lay, snap_lots, summary, sectors + [SectorEntry(0, sector)])
        return mapping.get(sector)

    def _template_lot_row(self, lay: _Layout, section: int, blk: Block | None) -> int | None:
        sec = lay.sections[section]
        cands = [r for r in range(sec.first_row, sec.subtotal_row) if lay.row_kind[r] == "lot"]
        if not cands:
            cands = [r for r in range(sec.first_row, sec.subtotal_row) if lay.row_kind[r] == "blank"]
        if blk is not None:
            inside = [r for r in cands if blk.first_row <= r <= blk.last_row]
            cands = inside or cands
        return cands[-1] if cands else None

    def _section_patterns(self, lay: _Layout, section: int) -> dict[int, str]:
        """Majority (R1C1-equivalent) formula per computed column across the data rows of a
        section; falls back to the whole sheet when the section has no data rows."""
        def collect(rows):
            votes: dict[int, Counter] = defaultdict(Counter)
            counts: Counter = Counter()
            for r in rows:
                counts["rows"] += 1
                for key, c in lay.col.items():
                    if key in ("name", "code", "date", "face_value", "qty", "rate", "charges", "market_rate"):
                        continue
                    f = lay.f(r, c)
                    if f:
                        votes[c][refs.canonical(f, refs.cell_name(r, c))] += 1
            return votes, counts["rows"]

        sec = lay.sections[section]
        rows = [r for r in range(sec.first_row, sec.subtotal_row) if lay.row_kind[r] in ("lot", "blank")]
        votes, n = collect(rows)
        if not votes:
            all_rows = [r for r, k in lay.row_kind.items() if k in ("lot", "blank")]
            votes, n = collect(all_rows)
        out = {}
        for c, cnt in votes.items():
            pat, hits = cnt.most_common(1)[0]
            if hits * 2 >= max(1, n) * 0.5:  # pattern used by at least a quarter of rows
                out[c] = pat
        # a column with formulas in the input set (e.g. held qty '=E4') stays a formula
        return out

    def _table_patterns(self, lay: _Layout, rows: list[int], cols: list[int]) -> dict[int, str]:
        out = {}
        for c in cols:
            cnt = Counter()
            for r in rows:
                f = lay.f(r, c)
                if f:
                    cnt[refs.canonical(f, refs.cell_name(r, c))] += 1
            if cnt:
                out[c] = cnt.most_common(1)[0][0]
        return out

    def _insert_table_row(self, sheet, lay: _Layout, at, template, specs, planned, nl, *, summary, sector):
        sc, xc = lay.sum_col, lay.sec_col
        first = (lay.sector_rows if sector else lay.summary_rows)[0]
        spec = refs.InsertSpec(at=at, count=1, extend_below=True, group=None)
        sheet.insert_rows(spec)
        specs.append(spec)
        idx = len(specs) - 1
        lay = discover(sheet, self.cfg)  # patterns after the shift (absolute refs updated)
        sum_rows = [r for r in lay.summary_rows if r != at]
        sec_rows = [r for r in lay.sector_rows if r != at]
        sum_pat = self._table_patterns(lay, sum_rows, [c for k, c in sc.items() if k not in ("name", "sector")]) if summary else {}
        sec_pat = self._table_patterns(lay, sec_rows, [c for k, c in xc.items() if k != "sector"]) if sector else {}
        sheet.create_row(at, template if template < at else template + 1)
        desc = []
        if summary:
            sheet.set_cell(at, sc["name"], nl.name)
            sheet.set_cell(at, sc["sector"], nl.sector)
            for c, p in sum_pat.items():
                sheet.set_cell(at, c, formula=refs.translate(p, refs.cell_name(100000, c), refs.cell_name(at, c)))
            desc.append(f"Summary row for '{nl.name}' (sector {nl.sector})")
        if sector:
            sheet.set_cell(at, xc["sector"], nl.sector)
            for c, p in sec_pat.items():
                sheet.set_cell(at, c, formula=refs.translate(p, refs.cell_name(100000, c), refs.cell_name(at, c)))
            desc.append(f"Sector table row for new sector '{nl.sector}'")
        kind = "summary" if summary and not sector else ("sector" if sector and not summary else "summary+sector")
        planned.append(PlannedRow(kind, at, "; ".join(desc), nl.ref, {"_spec_index": idx, "first_row": first}))

    # ------------------------------------------------------------------- verify
    def _verify(self, old_bytes, new_bytes, before: SheetSnapshot, before_cells, specs, lots, planned) -> list[dict]:
        checks = []
        wb2 = Workbook(new_bytes)
        sh2 = wb2.sheet(before.sheet_name)
        after_cells = sh2.cells()

        def map_row(r):
            for s in specs:
                if r >= s.at:
                    r += s.count
            return r

        def expected_formula(f, r):
            # replay the shifts on the original text, tracking the formula's own row
            for s in specs:
                f = refs.shift_formula(f, s, own_sheet=before.sheet_name, target_sheet=before.sheet_name, formula_row=r)
                if r >= s.at:
                    r += s.count
            return f

        bad = []
        for (r, c), (val, f) in before_cells.items():
            nr = map_row(r)
            cell = after_cells.get((nr, c))
            if cell is None:
                if val is BLANK and f is None:
                    continue
                bad.append(f"{refs.cell_name(r, c)} missing")
                continue
            if f is None:
                if cell.formula is not None or (cell.value != val and not (val is BLANK and cell.value is BLANK)):
                    bad.append(f"{refs.cell_name(r, c)} value changed {val!r} -> {cell.value!r}")
            else:
                if cell.formula != expected_formula(f, r):
                    bad.append(f"{refs.cell_name(r, c)} formula {f!r} -> {cell.formula!r}")
        checks.append({"name": "existing_cells_intact", "passed": not bad,
                       "detail": "All pre-existing cells preserved (only moved)" if not bad else "; ".join(bad[:5])})

        new_rows = {p.row for p in planned}
        moved_targets = {map_row(r) for (r, _c) in before_cells}
        overlap = sorted(new_rows & moved_targets)
        checks.append({"name": "no_overwrite", "passed": not overlap,
                       "detail": "New rows occupy fresh positions only" if not overlap else f"rows {overlap} overlap"})

        try:
            import openpyxl
            openpyxl.load_workbook(io.BytesIO(new_bytes))
            checks.append({"name": "workbook_loads", "passed": True, "detail": "Workbook re-opens cleanly"})
        except Exception as e:  # pragma: no cover
            checks.append({"name": "workbook_loads", "passed": False, "detail": str(e)})

        after = self._snapshot(discover(sh2, self.cfg))
        added = sum(round(l.qty * l.rate + round(l.charges, 2), 6) for l in lots)
        # sector table only counts holdings whose summary sector is listed in it
        sector_names = {name_key(x.name) for x in after.sectors}
        sector_of = {name_key(x.name): x.sector for x in after.summary if x.name}
        added_sector = sum(round(l.qty * l.rate + round(l.charges, 2), 6) for l in lots
                           if name_key(sector_of.get(name_key(l.name)) or "") in sector_names)
        for key, label, exp in (("grand_total_cost", "Grand Total purchase price", added),
                                ("summary_total", "Summary total", added),
                                ("sector_total", "Sector table total", added_sector)):
            b, a = before.totals.get(key), after.totals.get(key)
            if b is None or a is None:
                continue
            ok = abs((a - b) - exp) < 0.01
            checks.append({"name": f"{key}_delta", "passed": ok,
                           "detail": f"{label}: {b:,.2f} -> {a:,.2f} (expected +{exp:,.2f})"})
        ev = sh2.evaluator()
        errs = []
        for p in planned:
            if p.kind != "lot":
                continue
            for (r, c), cell in after_cells.items():
                if r == p.row and cell.formula is not None:
                    try:
                        if isinstance(ev.value(r, c), XLError):
                            errs.append(cell.coord)
                    except Unsupported:
                        pass
        checks.append({"name": "new_rows_evaluate", "passed": not errs,
                       "detail": "Formulas in new purchase rows evaluate without errors" if not errs
                       else f"Errors in {', '.join(errs[:6])}"})
        checks.append({"name": "lot_count", "passed": len(after.lots) == len(before.lots) + len(lots),
                       "detail": f"Purchase rows: {len(before.lots)} -> {len(after.lots)}"})
        return checks
