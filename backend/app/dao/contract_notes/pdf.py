"""PDF access shared by all contract-note parsers (decryption + text/table extraction)."""
from __future__ import annotations

import io
from dataclasses import dataclass

import pdfplumber
import pikepdf

from app.dao.base import ParseError, PasswordError

MAX_PAGES = 60


@dataclass
class OpenedPdf:
    pages_text: list[str]
    pages_tables: list[list[list[list[str | None]]]]
    encrypted: bool

    @property
    def text(self) -> str:
        return "\n".join(self.pages_text)

    def all_tables(self):
        for p, tables in enumerate(self.pages_tables):
            for t in tables:
                yield p, t


def open_pdf(data: bytes, password: str | None) -> OpenedPdf:
    if not data.startswith(b"%PDF"):
        raise ParseError("File is not a PDF")
    encrypted = False
    try:
        with pikepdf.open(io.BytesIO(data)) as pdf:
            encrypted = pdf.is_encrypted
    except pikepdf.PasswordError:
        encrypted = True
    except pikepdf.PdfError as e:
        raise ParseError(f"Unreadable PDF: {e}") from e
    if encrypted:
        if not password:
            raise PasswordError("This contract note is password protected; a password is required")
        try:
            with pikepdf.open(io.BytesIO(data), password=password) as pdf:
                out = io.BytesIO()
                pdf.save(out)  # saved without encryption
                data = out.getvalue()
        except pikepdf.PasswordError as e:
            raise PasswordError("Wrong password for this contract note") from e
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            if len(pdf.pages) > MAX_PAGES:
                raise ParseError(f"PDF has too many pages ({len(pdf.pages)})")
            texts, tables = [], []
            for page in pdf.pages:
                texts.append(page.extract_text() or "")
                tables.append(page.extract_tables() or [])
    except ParseError:
        raise
    except Exception as e:  # pdfminer raises a zoo of exceptions
        raise ParseError(f"Could not extract text from PDF: {e}") from e
    return OpenedPdf(pages_text=texts, pages_tables=tables, encrypted=encrypted)
