"""Domain models shared by every layer.

These types are deliberately independent of Excel and of any broker's PDF layout:
DAOs translate their storage format to/from these objects, services only speak these.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


# --------------------------------------------------------------------------- broker side
@dataclass
class TradeFill:
    """A single exchange execution from the trade-wise annexure."""

    order_no: str
    trade_no: str
    trade_time: str
    exchange: str
    isin: str
    security: str
    side: Side
    qty: float
    price: float


@dataclass
class Trade:
    """One security / side aggregated for a contract note (what becomes one sheet row)."""

    isin: str
    security_name: str  # as printed by the broker
    side: Side
    qty: float
    rate: float  # weighted average price before charges
    gross_value: float
    brokerage: float = 0.0
    gst: float = 0.0
    stt: float = 0.0
    other_levies: float = 0.0  # exchange txn + SEBI + stamp duty etc.
    net_value: float = 0.0  # value after all levies (what the client pays/receives)
    trade_date: dt.date | None = None
    fills: list[TradeFill] = field(default_factory=list)

    @property
    def charges(self) -> float:
        return round(self.brokerage + self.gst + self.stt + self.other_levies, 2)


@dataclass
class ChargeBreakdown:
    brokerage: float = 0.0
    stt: float = 0.0
    exchange_txn: float = 0.0
    sebi_fees: float = 0.0
    stamp_duty: float = 0.0
    cgst: float = 0.0
    sgst: float = 0.0
    igst: float = 0.0
    utt: float = 0.0
    other: float = 0.0

    @property
    def total(self) -> float:
        return round(sum(asdict(self).values()), 2)


@dataclass
class ContractNote:
    broker: str
    note_no: str
    trade_date: dt.date
    settlement_date: dt.date | None
    client_code: str | None
    trades: list[Trade]
    charges: ChargeBreakdown
    net_payable: float  # positive = client pays
    pay_in_obligation: float | None = None
    parser: str = ""
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------- sheet side
@dataclass
class Lot:
    """A purchase row in the tracking sheet's main area."""

    row: int
    name: str
    code: str | None
    date: dt.date | None
    face_value: float | None
    qty: float
    rate: float
    charges: float
    cost: float  # total purchase price
    held_qty: float  # market-value quantity (may include bonus shares)
    market_rate: float | None
    block: str | None  # heading of the block it sits under
    section: int
    sector: str | None = None  # resolved via the summary table


@dataclass
class SummaryEntry:
    row: int
    name: str
    sector: str | None
    units: float | None = None
    amount: float | None = None


@dataclass
class SectorEntry:
    row: int
    name: str
    amount: float | None = None
    pct: float | None = None


@dataclass
class Block:
    header: str | None
    header_row: int | None
    first_row: int
    last_row: int
    section: int


@dataclass
class Section:
    index: int
    first_row: int
    subtotal_row: int
    blocks: list[Block]
    title: str | None


@dataclass
class Issue:
    severity: str  # info | warning | error
    code: str
    message: str
    rows: list[int] = field(default_factory=list)


@dataclass
class SheetSnapshot:
    sheet_name: str
    valuation_date: dt.date | None
    lots: list[Lot]
    summary: list[SummaryEntry]
    sectors: list[SectorEntry]
    sections: list[Section]
    totals: dict[str, float | None]
    issues: list[Issue]
    blank_rows: list[int]
    sector_to_block: dict[str, str]

    def security_names(self) -> list[str]:
        seen: dict[str, None] = {}
        for lot in self.lots:
            seen.setdefault(lot.name, None)
        for s in self.summary:
            if s.name:
                seen.setdefault(s.name, None)
        return list(seen)

    def blocks(self) -> list[Block]:
        return [b for s in self.sections for b in s.blocks]


@dataclass
class NewLot:
    """A row the application wants to add."""

    name: str
    sector: str  # sector as used by the summary / sector tables
    date: dt.date
    qty: float
    rate: float
    charges: float
    code: str | None = None
    face_value: float | None = None
    market_rate: float | None = None
    block: str | None = None  # optional explicit target block heading
    ref: str | None = None  # caller correlation id


@dataclass
class PlannedRow:
    kind: str  # lot | block_header | summary | sector
    row: int  # row number in the final sheet
    description: str
    ref: str | None = None
    values: dict[str, Any] = field(default_factory=dict)


@dataclass
class AppendResult:
    rows: list[PlannedRow]
    checks: list[dict[str, Any]]
    warnings: list[str]
