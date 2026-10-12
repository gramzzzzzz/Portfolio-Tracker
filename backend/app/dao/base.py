"""Ports (interfaces) implemented by data-access objects.

Services depend only on these protocols. Swapping the spreadsheet layout, the broker or
the price source means writing a new DAO that satisfies the same protocol.
"""
from __future__ import annotations

import datetime as dt
from typing import Protocol, runtime_checkable

from app.domain.models import AppendResult, ContractNote, NewLot, PriceUpdateResult, SheetSnapshot


class LayoutError(Exception):
    """The workbook does not match the layout the DAO understands."""


class ParseError(Exception):
    """The document could not be parsed."""


class PasswordError(ParseError):
    """The PDF is encrypted and the password is missing or wrong."""


@runtime_checkable
class PortfolioSheetDAO(Protocol):
    def read(self, data: bytes) -> SheetSnapshot:
        """Parse the workbook into domain objects (no mutation)."""

    def append(self, data: bytes, lots: list[NewLot], *, dry_run: bool = False) -> tuple[bytes | None, AppendResult]:
        """Insert rows for ``lots``. Returns the new workbook bytes (None when dry_run)
        and a description of every row added plus verification results."""

    def update_prices(self, data: bytes, prices: dict[str, float], as_of: dt.date | None
                      ) -> tuple[bytes | None, PriceUpdateResult]:
        """Write current market rates into the market-rate input cells of EXISTING purchase
        rows (and the sheet's valuation date). Never touches formulas or adds rows.
        Returns None bytes when nothing changed."""


@runtime_checkable
class ContractNoteParser(Protocol):
    name: str

    def can_parse(self, text: str) -> bool:
        """Cheap check on the first page text."""

    def parse(self, pdf: "OpenedPdf") -> ContractNote:  # noqa: F821
        ...


@runtime_checkable
class PriceProvider(Protocol):
    name: str

    def quotes(self, symbols: list[str]) -> dict[str, dict]:
        """symbol -> {price, change_pct, high52, low52, time}"""

    def history(self, symbol: str, start: dt.date) -> list[tuple[dt.date, float]]:
        ...

    def resolve(self, query: str) -> list[dict]:
        """Look up candidate symbols for an ISIN / name."""
