"""Composition root: the only place where concrete DAOs are chosen and wired into services."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import Config
from app.dao.contract_notes.registry import ContractNoteReader
from app.dao.prices.yahoo import YahooPriceProvider
from app.dao.repo.blobs import BlobStore
from app.dao.repo.sqlite_repo import Repository
from app.dao.xlsx.invt_sheet import InvtSheetDAO
from app.services.analytics_service import AnalyticsService
from app.services.import_service import ImportService
from app.services.price_sync import PriceSyncService
from app.services.prices_service import PriceService
from app.services.settings_service import SettingsService
from app.services.sheet_lock import SheetLock
from app.services.tax_service import TaxService
from app.services.version_service import VersionService

log = logging.getLogger(__name__)


@dataclass
class Container:
    config: Config
    repo: Repository
    settings: SettingsService
    versions: VersionService
    imports: ImportService
    prices: PriceService
    analytics: AnalyticsService
    tax: TaxService
    price_sync: PriceSyncService


def build(config: Config, *, price_provider=..., sheet_dao=None, reader=None) -> Container:
    repo = Repository(config.db_path)
    settings = SettingsService(repo, config.secret_key, max(5, config.price_sync_minutes or 60))
    dao = sheet_dao or InvtSheetDAO()
    versions = VersionService(repo, BlobStore(config.blobs_dir, ".xlsx"), dao, SheetLock(config.lock_path),
                              mirror_dir=config.mirror_dir)
    if price_provider is ...:
        price_provider = YahooPriceProvider() if config.live_prices else None
    prices = PriceService(repo, price_provider)
    imports = ImportService(repo, versions, dao, reader or ContractNoteReader(),
                            BlobStore(config.uploads_dir, ".pdf"), settings, prices)
    analytics = AnalyticsService(repo, prices, settings)
    tax = TaxService(repo, analytics, settings)
    analytics.attach_tax(tax)
    price_sync = PriceSyncService(repo, versions, dao, prices, settings)
    versions.on_change = price_sync.trigger  # new rows / uploads get live rates soon
    if versions.head_id() is None and config.seed_sheet and config.seed_sheet.exists():
        try:
            versions.upload(config.seed_sheet.read_bytes(), config.seed_sheet.name, "Initial sheet (seed)")
            log.info("Seeded tracking sheet from %s", config.seed_sheet)
        except Exception as e:  # pragma: no cover
            log.error("Could not seed sheet: %s", e)
    return Container(config, repo, settings, versions, imports, prices, analytics, tax, price_sync)
