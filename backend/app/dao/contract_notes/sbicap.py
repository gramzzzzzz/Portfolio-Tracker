"""Contract-note DAO for SBICAP Securities (SBI Securities) common contract notes.

Everything specific to the SBICAP PDF layout lives here. Tables are located by their
header labels (not by page/position) so pagination changes do not break parsing.
"""
from __future__ import annotations

import datetime as dt
import re

from app.dao.base import ParseError
from app.domain.models import ChargeBreakdown, ContractNote, Side, Trade, TradeFill

from .pdf import OpenedPdf

ISIN_RE = re.compile(r"\b(IN[A-Z0-9]{9}\d)\b")
DESC_RE = re.compile(r"^(?P<name>.*?)-(?P<seg>[A-Za-z ]+)-(?P<isin>IN[A-Z0-9]{9}\d)$")


def _clean(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _num(s) -> float | None:
    s = _clean(s).replace(",", "")
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _date(s: str) -> dt.date | None:
    s = _clean(s).upper()
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


class SbicapParser:
    name = "sbicap"
    broker = "SBICAP Securities"

    def can_parse(self, text: str) -> bool:
        t = text.upper()
        return "SBICAP SECURITIES" in t and "CONTRACT NOTE" in t

    # ------------------------------------------------------------------- parse
    def parse(self, pdf: OpenedPdf) -> ContractNote:
        warnings: list[str] = []
        header = self._header(pdf)
        equity = self._equity_segment(pdf)
        scrips = self._scrip_summary(pdf)
        fills = self._fills(pdf)
        charges, total_payable, pay_in = self._obligation(pdf)

        if not equity and not scrips:
            raise ParseError("No equity transactions found in the contract note")

        trade_date = header["trade_date"]
        trades: list[Trade] = []
        isins = list(dict.fromkeys(list(equity) + list(scrips)))
        for isin in isins:
            eq = equity.get(isin, {})
            sc = scrips.get(isin, {})
            name = eq.get("name") or sc.get("name") or isin
            legs = []
            buy_qty = eq.get("buy_qty", sc.get("bought", 0.0)) or 0.0
            sell_qty = eq.get("sell_qty", sc.get("sold", 0.0)) or 0.0
            if buy_qty > 0:
                legs.append((Side.BUY, buy_qty, eq.get("buy_wap") or sc.get("avg_rate") or 0.0, eq.get("buy_brokerage", 0.0)))
            if sell_qty > 0:
                legs.append((Side.SELL, sell_qty, eq.get("sell_wap") or sc.get("avg_rate") or 0.0, eq.get("sell_brokerage", 0.0)))
            gross_total = sum(q * r for _, q, r, _ in legs) or 1.0
            for side, qty, wap, brok_ps in legs:
                gross = round(qty * wap, 2)
                share = gross / gross_total
                brokerage = sc.get("brokerage", brok_ps * qty) * share if sc else brok_ps * qty
                gst = sc.get("gst", 0.0) * share
                stt = sc.get("stt", 0.0) * share
                other = sc.get("other", 0.0) * share
                t = Trade(
                    isin=isin,
                    security_name=name,
                    side=side,
                    qty=qty,
                    rate=round(wap, 4),
                    gross_value=gross,
                    brokerage=round(brokerage, 4),
                    gst=round(gst, 4),
                    stt=round(stt, 4),
                    other_levies=round(other, 4),
                    trade_date=trade_date,
                    fills=[f for f in fills if f.isin == isin and f.side == side],
                )
                sign = 1 if side == Side.BUY else -1
                t.net_value = round(gross + sign * t.charges, 2)
                trades.append(t)

        if not scrips and trades:
            # no per-scrip levies: allocate note level charges by traded value
            levies = charges.total - charges.brokerage
            tot = sum(t.gross_value for t in trades) or 1.0
            for t in trades:
                t.other_levies = round(levies * t.gross_value / tot, 4)
                t.net_value = round(t.gross_value + (1 if t.side == Side.BUY else -1) * t.charges, 2)
            warnings.append("Per-scrip charges not found; note-level charges were allocated by traded value.")

        # ---- cross checks
        if total_payable is not None and trades:
            calc = sum(t.net_value if t.side == Side.BUY else -t.net_value for t in trades)
            if abs(calc - total_payable) > 0.05 * max(1, len(trades)):
                warnings.append(
                    f"Sum of per-scrip net values ({calc:,.2f}) differs from the note total ({total_payable:,.2f})."
                )
        for t in trades:
            if t.fills:
                fq = sum(f.qty for f in t.fills)
                if abs(fq - t.qty) > 1e-6:
                    warnings.append(f"{t.security_name}: trade-wise quantity {fq:g} != summary quantity {t.qty:g}")

        return ContractNote(
            broker=self.broker,
            note_no=header["note_no"],
            trade_date=trade_date,
            settlement_date=header.get("settlement_date"),
            client_code=header.get("client_code"),
            trades=trades,
            charges=charges,
            net_payable=total_payable if total_payable is not None else sum(t.net_value for t in trades),
            pay_in_obligation=pay_in,
            parser=self.name,
            warnings=warnings,
        )

    # ------------------------------------------------------------- sections
    def _header(self, pdf: OpenedPdf) -> dict:
        text = pdf.pages_text[0] if pdf.pages_text else ""
        out: dict = {}
        m = re.search(r"CONTRACT NOTE NO\.?\s*:?\s*(\d{6,})", text, re.I)
        if m:
            out["note_no"] = m.group(1)
        m = re.search(r"TRADE DATE\s*:?\s*(\d{1,2}-[A-Z]{3}-\d{2,4})", text, re.I)
        if m:
            out["trade_date"] = _date(m.group(1))
        m = re.search(r"SETTLEMENT\s*(?:\S+\s+)*?(\d{1,2}-[A-Z]{3}-\d{4})", text, re.I)
        if m:
            out["settlement_date"] = _date(m.group(1))
        m = re.search(r"UCC of Client\s*(\d+)", text, re.I)
        if m:
            out["client_code"] = m.group(1)
        # table fallback
        for _, t in pdf.all_tables():
            for row in t:
                cells = [_clean(c) for c in row]
                for i, c in enumerate(cells[:-1]):
                    if c.upper().startswith("CONTRACT NOTE NO") and "note_no" not in out and cells[i + 1]:
                        out["note_no"] = cells[i + 1]
                    if c.upper() == "TRADE DATE" and "trade_date" not in out:
                        out["trade_date"] = _date(cells[i + 1])
        if "note_no" not in out or not out.get("trade_date"):
            raise ParseError("Could not read contract note number / trade date")
        return out

    def _find_tables(self, pdf: OpenedPdf, *needles: str):
        for p, t in pdf.all_tables():
            head = " ".join(_norm(c) for r in t[:3] for c in r if c)
            if all(_norm(n) in head for n in needles):
                yield p, t

    def _equity_segment(self, pdf: OpenedPdf) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for _, t in self._find_tables(pdf, "ISIN", "WAP", "Net Obligation"):
            for row in t:
                cells = [_clean(c) for c in row]
                if not cells or not ISIN_RE.fullmatch(cells[0] or ""):
                    continue
                vals = [_num(c) for c in cells[2:]]
                if len(vals) < 12:
                    continue
                (bq, bwap, bbrok, _bwap_after, _btotal, sq, swap, sbrok, _swap_after, _stotal, _netq, _net) = vals[:12]
                out[cells[0]] = {
                    "name": cells[1],
                    "buy_qty": bq or 0.0,
                    "buy_wap": bwap or 0.0,
                    "buy_brokerage": bbrok or 0.0,
                    "sell_qty": sq or 0.0,
                    "sell_wap": swap or 0.0,
                    "sell_brokerage": sbrok or 0.0,
                }
        return out

    def _scrip_summary(self, pdf: OpenedPdf) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for _, t in self._find_tables(pdf, "Security Description", "Bought", "Net Total"):
            hdr = None
            for row in t:
                cells = [_clean(c) for c in row]
                if hdr is None:
                    if any(_norm(c).startswith("bought") for c in cells):
                        hdr = [_norm(c) for c in cells]
                    continue
                desc = next((c for c in cells if DESC_RE.match(c)), None)
                if not desc:
                    continue
                m = DESC_RE.match(desc)

                def col(prefix):
                    for i, h in enumerate(hdr):
                        if h.startswith(prefix):
                            return _num(cells[i]) or 0.0
                    return 0.0

                out[m.group("isin")] = {
                    "name": _clean(m.group("name")),
                    "bought": col("bought"),
                    "sold": col("sold"),
                    "avg_rate": col("averagerate"),
                    "gross": col("grosstotal"),
                    "brokerage": col("brokerage"),
                    "gst": col("gstonbrokerage"),
                    "stt": col("totalsecuritytransactiontax"),
                    "other": col("otherstatutorylevies"),
                    "net_after": col("nettotalafterlevies"),
                }
        return out

    def _fills(self, pdf: OpenedPdf) -> list[TradeFill]:
        out: list[TradeFill] = []
        for _, t in self._find_tables(pdf, "Order", "Trade", "Security/Contract Description"):
            exchange = ""
            for row in t:
                cells = [_clean(c) for c in row]
                if len(cells) >= 8 and cells[0] and not any(cells[1:]):
                    exchange = cells[0]
                    continue
                if len(cells) < 8 or not ISIN_RE.search(cells[4] or ""):
                    continue
                m = DESC_RE.match(cells[4])
                side = Side.BUY if cells[5].upper().startswith("B") else Side.SELL
                out.append(
                    TradeFill(
                        order_no=cells[0],
                        trade_no=cells[2],
                        trade_time=cells[3],
                        exchange=exchange,
                        isin=m.group("isin") if m else ISIN_RE.search(cells[4]).group(1),
                        security=_clean(m.group("name")) if m else cells[4],
                        side=side,
                        qty=_num(cells[6]) or 0.0,
                        price=_num(cells[7]) or 0.0,
                    )
                )
        return out

    def _obligation(self, pdf: OpenedPdf) -> tuple[ChargeBreakdown, float | None, float | None]:
        ch = ChargeBreakdown()
        total = None
        pay_in = None
        for _, t in self._find_tables(pdf, "PAY IN", "OBLIGATION"):
            current_tax = None
            for row in t:
                cells = [_clean(c) for c in row]
                label = _norm(cells[0])
                sub = _norm(cells[1]) if len(cells) > 1 else ""
                value = _num(cells[-1])
                if label in ("cgst", "sgst", "igst", "utt"):
                    current_tax = label
                if sub.startswith("amount") and current_tax:
                    setattr(ch, current_tax, value or 0.0)
                    continue
                if label.startswith("payinpayoutobligation"):
                    pay_in = value
                elif label.startswith("securitytransactiontax"):
                    ch.stt = value or 0.0
                elif label.startswith("exchangetransactioncharges"):
                    ch.exchange_txn = value or 0.0
                elif label.startswith("sebiturnoverfees"):
                    ch.sebi_fees = value or 0.0
                elif label.startswith("stampduty"):
                    ch.stamp_duty = value or 0.0
                elif label == "brokerage":
                    ch.brokerage = value or 0.0
                elif label == "total":
                    total = value
        return ch, total, pay_in
