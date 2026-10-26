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
    # set by the positions engine (sells / bonus / splits from the Transactions sheet)
    origin: str = "purchase"  # purchase | bonus
    txn_id: str | None = None  # bonus lots: the Transactions row that created it
    orig_qty: float | None = None  # sheet quantity of the purchase row (before sells/splits)
    orig_cost: float | None = None  # sheet Total Purchase Price of the row
    orig_charges: float | None = None  # sheet charges (Comn.) of the row


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


@dataclass
class PriceUpdateResult:
    """Outcome of refreshing market rates in the sheet."""

    changed: bool
    updated_rows: list[int]
    unchanged_rows: list[int]
    skipped_formula_rows: list[int]  # market-rate cell holds a formula -> never overwritten
    missing: list[str]  # holdings without a price
    valuation_cell: str | None
    valuation_date: dt.date | None
    checks: list[dict[str, Any]]


# ------------------------------------------------------------------ app-owned sheets
class TxnType(str, Enum):
    SELL = "SELL"
    BONUS = "BONUS"  # free shares allotted: qty = shares received
    SPLIT = "SPLIT"  # ratio = new shares per old share (e.g. 5 for a 1:5 split)


class AssetClass(str, Enum):
    """Provision for every investment type. Only SHARE/ETF (main sheet) and GOLD are
    implemented today; the others are accepted by the data model so they can be added
    without schema changes."""

    SHARE = "share"
    ETF = "etf"
    MUTUAL_FUND = "mutual_fund"
    GOLD = "physical_gold"
    FD = "fd"
    RD = "rd"
    OTHER = "other"


@dataclass
class Transaction:
    """A row of the app-owned ``Transactions`` sheet (events that are not purchases)."""

    id: str
    date: dt.date
    type: TxnType
    name: str  # security name exactly as in the tracking sheet
    qty: float = 0.0  # SELL: units sold; BONUS: units received
    price: float | None = None  # SELL: price per unit
    charges: float = 0.0  # SELL: brokerage + exchange + SEBI + GST + stamp (deductible)
    stt: float = 0.0  # SELL: STT (not deductible)
    ratio: float | None = None  # SPLIT: new units per old unit
    isin: str | None = None
    asset_class: str = AssetClass.SHARE.value
    broker: str | None = None
    note_no: str | None = None
    source: str = "manual"  # manual | contract_note
    remarks: str | None = None
    row: int | None = None

    @property
    def proceeds(self) -> float:
        return round((self.price or 0.0) * self.qty, 2)


@dataclass
class GoldItem:
    """A row of the app-owned ``Gold`` sheet (physical gold, entered by hand)."""

    id: str
    date: dt.date
    description: str
    form: str  # coin | bar | jewellery
    purity: str  # 24K | 22K | 18K | 14K | custom like "0.916"
    weight_g: float
    amount_paid: float  # total paid incl. making charges and GST
    making_charges: float = 0.0  # included in amount_paid (informational)
    gst_paid: float = 0.0  # included in amount_paid (informational)
    seller: str | None = None
    sold_date: dt.date | None = None
    sold_amount: float | None = None
    remarks: str | None = None
    # written by the app (price sync) so the sheet is readable on its own
    rate_per_g: float | None = None
    current_value: float | None = None
    value_date: dt.date | None = None
    row: int | None = None


@dataclass
class Extras:
    transactions: list[Transaction]
    gold: list[GoldItem]
    warnings: list[str]
