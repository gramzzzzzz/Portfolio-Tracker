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
from app.services.email_sync_service import EmailSyncService
from app.services.gold_service import GoldService
from app.services.history_service import HistoryService
from app.services.performance_service import PerformanceService
from app.services.import_service import ImportService
from app.dao.xlsx.extra_sheets import ExtraSheetsDAO
from app.services.portfolio_service import PortfolioService
from app.services.price_sync import PriceSyncService
from app.services.prices_service import PriceService
from app.services.settings_service import SettingsService
from app.services.sheet_lock import SheetLock
from app.services.tax_service import TaxService
from app.services.transactions_service import TransactionsService
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
    portfolio: PortfolioService
    extras_dao: ExtraSheetsDAO
    transactions: TransactionsService
    gold: GoldService
    history: HistoryService
    performance: PerformanceService
    email: EmailSyncService


def build(config: Config, *, price_provider=..., sheet_dao=None, reader=None, gmail=None) -> Container:
    repo = Repository(config.db_path)
    settings = SettingsService(repo, config.secret_key, max(5, config.price_sync_minutes or 60))
    dao = sheet_dao or InvtSheetDAO()
    versions = VersionService(repo, BlobStore(config.blobs_dir, ".xlsx"), dao, SheetLock(config.lock_path),
                              mirror_dir=config.mirror_dir)
    if price_provider is ...:
        price_provider = YahooPriceProvider() if config.live_prices else None
    prices = PriceService(repo, price_provider)
    extras_dao = ExtraSheetsDAO()
    versions.extras_dao = extras_dao
    portfolio = PortfolioService(versions, extras_dao)
    imports = ImportService(repo, versions, dao, reader or ContractNoteReader(),
                            BlobStore(config.uploads_dir, ".pdf"), settings, prices, portfolio, extras_dao)
    analytics = AnalyticsService(repo, prices, settings)
    tax = TaxService(repo, analytics, settings, portfolio)
    analytics.attach_portfolio(portfolio)
    analytics.attach_tax(tax)
    price_sync = PriceSyncService(repo, versions, dao, prices, settings)
    versions.on_change = price_sync.trigger  # new rows / uploads get live rates soon
    if versions.head_id() is None and config.seed_sheet and config.seed_sheet.exists():
        try:
            versions.upload(config.seed_sheet.read_bytes(), config.seed_sheet.name, "Initial sheet (seed)")
            log.info("Seeded tracking sheet from %s", config.seed_sheet)
        except Exception as e:  # pragma: no cover
            log.error("Could not seed sheet: %s", e)
    transactions = TransactionsService(versions, extras_dao, portfolio)
    gold = GoldService(repo, versions, extras_dao, portfolio, prices, settings,
                       BlobStore(config.data_dir / "photos", ".img"))
    gold.tax = tax
    tax.gold = gold
    price_sync.gold = gold
    history = HistoryService(portfolio)
    performance = PerformanceService(portfolio, prices, analytics, settings)
    email = EmailSyncService(repo, settings, imports, config.data_dir / "tmp", config.max_upload_mb * 1024 * 1024,
                             gmail)
    return Container(config, repo, settings, versions, imports, prices, analytics, tax, price_sync, portfolio,
                     extras_dao, transactions, gold, history, performance, email)
