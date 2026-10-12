"""Application configuration (environment driven)."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    data_dir: Path
    seed_sheet: Path | None
    secret_key: bytes
    session_hours: int
    max_upload_mb: int
    live_prices: bool
    static_dir: Path | None
    cookie_secure: bool
    price_sync_minutes: int = 60  # default for the setting; 0 disables the background sync thread

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def blobs_dir(self) -> Path:
        return self.data_dir / "versions"

    @property
    def mirror_dir(self) -> Path:
        return self.data_dir / "current"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / ".sheet.lock"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "contract_notes"


def _bool(v: str | None, default: bool) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def load_config() -> Config:
    data_dir = Path(os.environ.get("PT_DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    key_file = data_dir / "secret.key"
    if os.environ.get("PT_SECRET_KEY"):
        key = os.environ["PT_SECRET_KEY"].encode()
    else:
        if not key_file.exists():
            key_file.write_bytes(secrets.token_bytes(32).hex().encode())
            os.chmod(key_file, 0o600)
        key = key_file.read_bytes().strip()
    seed = os.environ.get("PT_SEED_SHEET")
    static = os.environ.get("PT_STATIC_DIR")
    default_static = Path(__file__).resolve().parent.parent / "static"
    return Config(
        data_dir=data_dir,
        seed_sheet=Path(seed) if seed else None,
        secret_key=key,
        session_hours=int(os.environ.get("PT_SESSION_HOURS", "168")),
        max_upload_mb=int(os.environ.get("PT_MAX_UPLOAD_MB", "15")),
        live_prices=_bool(os.environ.get("PT_LIVE_PRICES"), True),
        static_dir=Path(static) if static else (default_static if default_static.exists() else None),
        cookie_secure=_bool(os.environ.get("PT_COOKIE_SECURE"), False),
        price_sync_minutes=int(os.environ.get("PT_PRICE_SYNC_MINUTES", "60")),
    )
