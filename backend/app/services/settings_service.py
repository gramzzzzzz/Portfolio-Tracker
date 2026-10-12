"""Typed application settings and encrypted secrets."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.dao.repo.sqlite_repo import Repository

DEFAULTS: dict[str, Any] = {
    "benchmark_symbol": "^NSEI",
    "price_sync_minutes": 60,
    "max_holding_pct": 20.0,
    "max_sector_pct": 35.0,
    # ---- tax (FY 2026-27 rates; editable)
    "stcg_rate_pct": 20.0,
    "ltcg_rate_pct": 12.5,
    "ltcg_exemption": 125000.0,
    "slab_rate_pct": 30.0,
    "surcharge_pct": 0.0,
    "cess_pct": 4.0,
    # gains already booked in the FY named by realized_fy (ignored once that FY is over)
    "realized_fy": "",
    "realized_stcg_fy": 0.0,
    "realized_ltcg_fy": 0.0,
    "realized_st_other_fy": 0.0,
    "realized_lt_other_fy": 0.0,
    # ---- selling costs: None = derive from imported contract notes
    "sell_expense_pct": None,
    "dp_charge_per_sale": 0.0,
    "upcoming_lt_window_days": 60,
    # sector -> tax class: equity | non_equity (gold/silver ETFs, intl funds) | debt
    "tax_class_by_sector": {},
    # sector -> block heading in the main area, overrides automatic mapping
    "sector_block_overrides": {},
}
_NULLABLE = {"sell_expense_pct"}
_SIGNED = {"realized_stcg_fy", "realized_ltcg_fy", "realized_st_other_fy", "realized_lt_other_fy"}  # losses allowed

_NUMERIC = {k for k, v in DEFAULTS.items() if isinstance(v, (int, float)) and not isinstance(v, bool)} | _NULLABLE


class SettingsService:
    def __init__(self, repo: Repository, secret_key: bytes, price_sync_minutes: int = 60):
        self.repo = repo
        self._fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"pdf-pw:" + secret_key).digest()))
        self._defaults = dict(DEFAULTS, price_sync_minutes=price_sync_minutes)

    def all(self) -> dict[str, Any]:
        stored = self.repo.get("settings", {}) or {}
        out = dict(self._defaults)
        out.update({k: v for k, v in stored.items() if k in self._defaults})
        out["has_pdf_password"] = self.repo.get_secret("pdf_password") is not None
        return out

    def get(self, key: str) -> Any:
        return self.all()[key]

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        stored = self.repo.get("settings", {}) or {}
        for k, v in patch.items():
            if k not in self._defaults:
                raise ValueError(f"Unknown setting '{k}'")
            if k in _NULLABLE and (v is None or v == ""):
                v = None
            elif k in _NUMERIC:
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    raise ValueError(f"'{k}' must be a number") from None
                if v < 0 and k not in _SIGNED:
                    raise ValueError(f"'{k}' must be >= 0")
                if k == "price_sync_minutes" and v < 5:
                    raise ValueError("Price sync interval must be at least 5 minutes")
            elif isinstance(self._defaults[k], bool):
                v = bool(v)
            elif isinstance(self._defaults[k], dict):
                if not isinstance(v, dict):
                    raise ValueError(f"'{k}' must be an object")
                v = {str(a): str(b) for a, b in v.items() if str(b).strip()}
            elif isinstance(self._defaults[k], str):
                v = str(v).strip()
            stored[k] = v
        self.repo.put("settings", stored)
        return self.all()

    # ------------------------------------------------------------ pdf password
    def set_pdf_password(self, password: str | None) -> None:
        self.repo.put_secret("pdf_password", self._fernet.encrypt(password.encode()) if password else None)

    def pdf_password(self) -> str | None:
        blob = self.repo.get_secret("pdf_password")
        if blob is None:
            return None
        try:
            return self._fernet.decrypt(blob).decode()
        except InvalidToken:
            return None

    # ------------------------------------------------------------ app login
    def has_login(self) -> bool:
        return self.repo.get("auth") is not None

    def set_login_password(self, password: str) -> None:
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters")
        salt = os.urandom(16)
        digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
        self.repo.put("auth", {"salt": salt.hex(), "hash": digest.hex()})

    def check_login_password(self, password: str) -> bool:
        a = self.repo.get("auth")
        if not a:
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(a["salt"]), n=2**14, r=8, p=1)
        return hmac.compare_digest(digest.hex(), a["hash"])
