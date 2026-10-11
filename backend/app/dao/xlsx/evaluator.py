"""Minimal, side-effect-free Excel formula evaluator.

Used for two purposes only:
  1. Reading computed values (e.g. holding quantity column K may be ``=E22+50``) when
     the workbook has no fresh cached values (openpyxl/our writer never runs Excel).
  2. Post-write verification (totals reconcile, new rows are included in aggregates) and
     refreshing cached ``<v>`` values so non-recalculating viewers show correct numbers.

It intentionally supports only the subset of Excel used by simple tracking sheets.
Unsupported constructs raise :class:`Unsupported`, and callers degrade gracefully.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from typing import Any, Callable

from openpyxl.formula.tokenizer import Token, Tokenizer
from openpyxl.utils import column_index_from_string

from .refs import CELL_RE


class Unsupported(Exception):
    pass


class XLError(str):
    """An Excel error value such as '#DIV/0!'."""


DIV0 = XLError("#DIV/0!")
VALUE = XLError("#VALUE!")
NA = XLError("#N/A")
REF = XLError("#REF!")


class Blank:
    _inst = None

    def __new__(cls):
        if cls._inst is None:
            cls._inst = super().__new__(cls)
        return cls._inst

    def __repr__(self):
        return "Blank"


BLANK = Blank()


class RangeVal:
    """A rectangular block of values (row-major)."""

    def __init__(self, rows: list[list[Any]]):
        self.rows = rows

    def flat(self):
        for r in self.rows:
            yield from r


def _to_num(v: Any):
    if isinstance(v, XLError):
        return v
    if v is BLANK or v is None:
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(",", ""))
        except ValueError:
            return VALUE
    if isinstance(v, RangeVal):
        # implicit intersection is not supported; take single-cell ranges only
        cells = list(v.flat())
        if len(cells) == 1:
            return _to_num(cells[0])
        return VALUE
    return VALUE


def _scalar(v: Any):
    if isinstance(v, RangeVal):
        cells = list(v.flat())
        return cells[0] if len(cells) == 1 else VALUE
    return v


_CRIT_RE = re.compile(r"^(<=|>=|<>|<|>|=)?(.*)$", re.S)


def _criterion(crit: Any) -> Callable[[Any], bool]:
    crit = _scalar(crit)
    if crit is BLANK or crit is None:
        # Excel treats an empty criteria cell as 0 (does not match blank cells)
        return lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v == 0
    if isinstance(crit, (int, float)) and not isinstance(crit, bool):
        return lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and float(v) == float(crit)
    s = str(crit)
    op, rhs = _CRIT_RE.match(s).groups()
    op = op or "="
    try:
        num = float(rhs)
    except ValueError:
        num = None
    if num is not None and op in ("<", ">", "<=", ">=", "=", "<>"):
        def numcmp(v):
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return _cmp(float(v), num, op)
            return op == "<>"
        return numcmp
    pattern = re.compile(
        "^" + re.escape(rhs.lower()).replace(r"\*", ".*").replace(r"\?", ".") + "$", re.S
    )

    def strcmp(v):
        sv = "" if v is BLANK or v is None else str(v)
        hit = bool(pattern.match(sv.lower()))
        if op == "=":
            return hit if rhs != "" else (v is BLANK or sv == "")
        if op == "<>":
            return not hit
        return _cmp(sv.lower(), rhs.lower(), op)

    return strcmp


def _cmp(a, b, op):
    return {
        "=": a == b,
        "<>": a != b,
        "<": a < b,
        ">": a > b,
        "<=": a <= b,
        ">=": a >= b,
    }[op]


def _excel_round(x: float, n: int) -> float:
    m = 10 ** n
    return math.floor(abs(x) * m + 0.5) / m * (1 if x >= 0 else -1)


def _nums(args):
    for a in args:
        if isinstance(a, RangeVal):
            for v in a.flat():
                if isinstance(v, XLError):
                    yield v
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    yield float(v)
        else:
            n = _to_num(a)
            yield n


def _first_error(seq):
    for v in seq:
        if isinstance(v, XLError):
            return v
    return None


def fn_sum(args):
    vals = list(_nums(args))
    e = _first_error(vals)
    return e if e else float(sum(vals))


def fn_sumif(args):
    if len(args) not in (2, 3):
        raise Unsupported("SUMIF arity")
    rng, crit = args[0], args[1]
    sum_rng = args[2] if len(args) == 3 else rng
    if not isinstance(rng, RangeVal) or not isinstance(sum_rng, RangeVal):
        return VALUE
    pred = _criterion(crit)
    total = 0.0
    sr = sum_rng.rows
    for i, row in enumerate(rng.rows):
        for j, v in enumerate(row):
            if pred(v):
                try:
                    sv = sr[i][j]
                except IndexError:
                    continue
                if isinstance(sv, XLError):
                    return sv
                if isinstance(sv, (int, float)) and not isinstance(sv, bool):
                    total += float(sv)
    return total


def fn_countif(args):
    rng, crit = args
    pred = _criterion(crit)
    return float(sum(1 for v in rng.flat() if pred(v)))


def fn_if(args):
    cond = _scalar(args[0])
    if isinstance(cond, XLError):
        return cond
    truthy = bool(_to_num(cond)) if not isinstance(cond, str) else bool(cond)
    if truthy:
        return args[1] if len(args) > 1 else True
    return args[2] if len(args) > 2 else False


def fn_iferror(args):
    v = _scalar(args[0])
    return args[1] if isinstance(v, XLError) else v


def fn_round(args):
    x = _to_num(args[0])
    n = _to_num(args[1]) if len(args) > 1 else 0.0
    if isinstance(x, XLError):
        return x
    return _excel_round(x, int(n))


def fn_minmax(f):
    def inner(args):
        vals = list(_nums(args))
        e = _first_error(vals)
        if e:
            return e
        return f(vals) if vals else 0.0
    return inner


def fn_average(args):
    vals = list(_nums(args))
    e = _first_error(vals)
    if e:
        return e
    return sum(vals) / len(vals) if vals else DIV0


def fn_abs(args):
    x = _to_num(args[0])
    return x if isinstance(x, XLError) else abs(x)


def fn_today(args):
    return float((dt.date.today() - dt.date(1899, 12, 30)).days)


FUNCS: dict[str, Callable] = {
    "SUM": fn_sum,
    "SUMIF": fn_sumif,
    "COUNTIF": fn_countif,
    "IF": fn_if,
    "IFERROR": fn_iferror,
    "ROUND": fn_round,
    "MIN": fn_minmax(min),
    "MAX": fn_minmax(max),
    "AVERAGE": fn_average,
    "ABS": fn_abs,
    "TODAY": fn_today,
}

_PREC = {
    "=": 1, "<>": 1, "<": 1, ">": 1, "<=": 1, ">=": 1,
    "&": 2,
    "+": 3, "-": 3,
    "*": 4, "/": 4,
    "^": 5,
}


class Evaluator:
    """Evaluates formulas of a single sheet.

    ``get_cell(row, col)`` must return a tuple ``(value, formula_or_None)`` where value is a
    Python number/str/bool/BLANK.
    """

    def __init__(self, get_cell: Callable[[int, int], tuple[Any, str | None]], max_row: int, max_col: int):
        self._get = get_cell
        self._cache: dict[tuple[int, int], Any] = {}
        self._active: set[tuple[int, int]] = set()
        self.max_row = max_row
        self.max_col = max_col

    # ---- public -----------------------------------------------------------
    def value(self, row: int, col: int):
        key = (row, col)
        if key in self._cache:
            return self._cache[key]
        raw, formula = self._get(row, col)
        if formula is None:
            return raw
        if key in self._active:
            raise Unsupported(f"circular reference at {key}")
        self._active.add(key)
        try:
            res = self.evaluate(formula)
        finally:
            self._active.discard(key)
        res = _scalar(res)
        if res is BLANK:
            res = 0.0
        self._cache[key] = res
        return res

    def evaluate(self, formula: str):
        f = formula if formula.startswith("=") else "=" + formula
        items = [t for t in Tokenizer(f).items if t.type != Token.WSPACE]
        saved = (getattr(self, "_toks", None), getattr(self, "_pos", 0))  # re-entrancy
        self._toks = items
        self._pos = 0
        try:
            v = self._expr(0)
            if self._pos != len(self._toks):
                raise Unsupported(f"trailing tokens in {formula}")
            return v
        finally:
            self._toks, self._pos = saved

    # ---- parser -------------------------------------------------------------
    def _peek(self):
        return self._toks[self._pos] if self._pos < len(self._toks) else None

    def _next(self):
        t = self._toks[self._pos]
        self._pos += 1
        return t

    def _expr(self, min_prec):
        left = self._unary()
        while True:
            t = self._peek()
            if t is None or t.type != Token.OP_IN:
                break
            op = t.value
            prec = _PREC.get(op)
            if prec is None:
                raise Unsupported(f"operator {op}")
            if prec < min_prec:
                break
            self._next()
            right = self._expr(prec + 1 if op != "^" else prec)
            left = self._binop(op, left, right)
        return left

    def _unary(self):
        t = self._peek()
        if t is not None and t.type == Token.OP_PRE:
            self._next()
            v = self._unary()
            n = _to_num(v)
            if isinstance(n, XLError):
                return n
            return -n if t.value == "-" else n
        v = self._primary()
        while True:
            t = self._peek()
            if t is not None and t.type == Token.OP_POST and t.value == "%":
                self._next()
                n = _to_num(v)
                v = n if isinstance(n, XLError) else n / 100.0
            else:
                break
        return v

    def _primary(self):
        t = self._next()
        if t.type == Token.OPERAND:
            if t.subtype == Token.NUMBER:
                return float(t.value)
            if t.subtype == Token.TEXT:
                return t.value[1:-1].replace('""', '"')
            if t.subtype == Token.LOGICAL:
                return t.value.upper() == "TRUE"
            if t.subtype == Token.ERROR:
                return XLError(t.value)
            if t.subtype == Token.RANGE:
                return self._ref(t.value)
            raise Unsupported(f"operand {t.value}")
        if t.type == Token.PAREN and t.subtype == Token.OPEN:
            v = self._expr(0)
            close = self._next()
            if close.type != Token.PAREN:
                raise Unsupported("paren")
            return v
        if t.type == Token.FUNC and t.subtype == Token.OPEN:
            name = t.value[:-1].upper()
            if name.startswith("_XLFN."):
                name = name[6:]
            args = []
            nt = self._peek()
            if nt is not None and nt.type == Token.FUNC and nt.subtype == Token.CLOSE:
                self._next()
            else:
                while True:
                    args.append(self._expr(0))
                    sep = self._next()
                    if sep.type == Token.SEP and sep.subtype == Token.ARG:
                        continue
                    if sep.type == Token.FUNC and sep.subtype == Token.CLOSE:
                        break
                    raise Unsupported("function args")
            fn = FUNCS.get(name)
            if fn is None:
                raise Unsupported(f"function {name}")
            return fn(args)
        raise Unsupported(f"token {t.value!r}")

    def _ref(self, ref: str):
        if "!" in ref:
            raise Unsupported("cross-sheet reference")
        ref = ref.replace("$", "")
        if ":" in ref:
            a, b = ref.split(":", 1)
            ma, mb = CELL_RE.match(a), CELL_RE.match(b)
            if not (ma and mb):
                raise Unsupported(f"range {ref}")
            r1, c1 = int(ma.group(4)), column_index_from_string(ma.group(2).upper())
            r2, c2 = int(mb.group(4)), column_index_from_string(mb.group(2).upper())
            r1, r2 = min(r1, r2), max(r1, r2)
            c1, c2 = min(c1, c2), max(c1, c2)
            return RangeVal([[self.value(r, c) for c in range(c1, c2 + 1)] for r in range(r1, r2 + 1)])
        m = CELL_RE.match(ref)
        if not m:
            raise Unsupported(f"ref {ref}")
        return self.value(int(m.group(4)), column_index_from_string(m.group(2).upper()))

    # ---- operators ----------------------------------------------------------
    def _binop(self, op, a, b):
        a, b = _scalar(a), _scalar(b)
        if isinstance(a, XLError):
            return a
        if isinstance(b, XLError):
            return b
        if op == "&":
            return ("" if a is BLANK else _fmt(a)) + ("" if b is BLANK else _fmt(b))
        if op in ("=", "<>", "<", ">", "<=", ">="):
            av = 0.0 if a is BLANK else a
            bv = 0.0 if b is BLANK else b
            if isinstance(av, str) and isinstance(bv, str):
                return _cmp(av.lower(), bv.lower(), op)
            if isinstance(av, str) != isinstance(bv, str):
                return _cmp(isinstance(av, str), isinstance(bv, str), op)
            return _cmp(float(av), float(bv), op)
        x, y = _to_num(a), _to_num(b)
        if isinstance(x, XLError):
            return x
        if isinstance(y, XLError):
            return y
        if op == "+":
            return x + y
        if op == "-":
            return x - y
        if op == "*":
            return x * y
        if op == "/":
            return DIV0 if y == 0 else x / y
        if op == "^":
            try:
                return x ** y
            except (OverflowError, ZeroDivisionError):
                return DIV0
        raise Unsupported(op)


def _fmt(v):
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)
