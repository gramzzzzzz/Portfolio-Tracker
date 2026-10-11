"""Chooses the right contract-note parser for a PDF.

To support another broker: implement the ContractNoteParser protocol in a new module and
add an instance to PARSERS. Nothing else in the application changes.
"""
from __future__ import annotations

from app.dao.base import ContractNoteParser, ParseError
from app.domain.models import ContractNote

from .pdf import open_pdf
from .sbicap import SbicapParser

PARSERS: list[ContractNoteParser] = [SbicapParser()]


class ContractNoteReader:
    def __init__(self, parsers: list[ContractNoteParser] | None = None):
        self.parsers = parsers or PARSERS

    def read(self, data: bytes, password: str | None) -> ContractNote:
        pdf = open_pdf(data, password)
        head = "\n".join(pdf.pages_text[:2])
        for p in self.parsers:
            if p.can_parse(head):
                return p.parse(pdf)
        raise ParseError("Unsupported contract note format (no parser recognised this broker)")

    @property
    def supported(self) -> list[str]:
        return [getattr(p, "broker", p.name) for p in self.parsers]
