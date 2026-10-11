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
    "live_prices": True,
    "benchmark_symbol": "^NSEI",
    "max_holding_pct": 20.0,
    "max_sector_pct": 35.0,
    "stcg_rate_pct": 20.0,
    "ltcg_rate_pct": 12.5,
    "ltcg_exemption": 125000.0,
    "slab_rate_pct": 30.0,
    "realized_ltcg_fy": 0.0,
    "realized_stcg_fy": 0.0,
    "upcoming_lt_window_days": 60,
    # sector -> tax class: equity | non_equity (gold/silver ETFs, intl funds) | debt
    "tax_class_by_sector": {},
    # sector -> block heading in the main area, overrides automatic mapping
    "sector_block_overrides": {},
}

_NUMERIC = {k for k, v in DEFAULTS.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


class SettingsService:
    def __init__(self, repo: Repository, secret_key: bytes, live_default: bool = True):
        self.repo = repo
        self._fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"pdf-pw:" + secret_key).digest()))
        self._defaults = dict(DEFAULTS, live_prices=live_default)

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
            if k in _NUMERIC:
                v = float(v)
                if v < 0:
                    raise ValueError(f"'{k}' must be >= 0")
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
