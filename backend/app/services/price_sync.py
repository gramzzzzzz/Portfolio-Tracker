"""Periodic market-price refresh of the tracking sheet.

At startup and then every ``price_sync_minutes`` (default 60) the service:
  1. resolves each holding to an exchange symbol and fetches quotes (network, NO lock held);
  2. takes the sheet lock briefly, re-reads HEAD, writes the market-rate cells of existing
     purchase rows + the valuation date through the sheet DAO (which verifies the edit), and
     swaps HEAD's content in place (not versioned, by the owner's choice);
  3. records the outcome so the UI can show "prices written to sheet at hh:mm".

Nothing is written when no price changed (e.g. nights / weekends), so the sheet's file only
changes while the market moves. Failures are logged and retried at the next tick.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time

from app.dao.base import PortfolioSheetDAO
from app.dao.repo.sqlite_repo import Repository
from app.domain.models import PriceUpdateResult
from app.services.prices_service import PriceService
from app.services.settings_service import SettingsService
from app.services.version_service import NoSheetError, VersionService

log = logging.getLogger(__name__)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
MIN_INTERVAL = 5


class PriceSyncService:
    STATUS_KEY = "price_sync_status"

    def __init__(self, repo: Repository, versions: VersionService, dao: PortfolioSheetDAO,
                 prices: PriceService, settings: SettingsService):
        self.repo = repo
        self.versions = versions
        self.dao = dao
        self.prices = prices
        self.settings = settings
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_lock = threading.Lock()  # one sync at a time
        self.gold = None  # GoldService (attached by the container)

    @property
    def enabled(self) -> bool:
        return self.prices.provider is not None

    def interval_minutes(self) -> int:
        return max(MIN_INTERVAL, int(self.settings.get("price_sync_minutes")))

    def status(self) -> dict:
        st = self.repo.get(self.STATUS_KEY, {}) or {}
        st["enabled"] = self.enabled
        st["interval_minutes"] = self.interval_minutes()
        return st

    # ------------------------------------------------------------------ one run
    def run_once(self, reason: str = "scheduled") -> dict:
        with self._run_lock:
            started = time.time()
            st = {"last_run": started, "reason": reason}
            if not self.enabled:
                st.update(result="disabled", detail="Live prices are turned off")
                return self._save(st)
            try:
                snap = self.versions.snapshot()
            except NoSheetError:
                st.update(result="no_sheet", detail="No sheet uploaded yet")
                return self._save(st)
            try:
                names = sorted({l.name for l in snap.lots})
                codes: dict[str, str | None] = {}
                for l in snap.lots:
                    codes.setdefault(l.name, l.code)
                symbols = self.prices.symbol_map(names, codes, auto_resolve=True)
                quotes = self.prices.quotes([s for s in symbols.values() if s])
                price_by_name = {n: quotes[s]["price"] for n, s in symbols.items() if s and s in quotes}
                times = [quotes[s]["time"] for s in symbols.values() if s in quotes and quotes[s].get("time")]
                as_of = dt.datetime.fromtimestamp(max(times), IST).date() if times else None
                if self.gold is not None:
                    self.gold.price()  # warm the quote cache outside the lock
                if not price_by_name and self.gold is None:
                    st.update(result="no_quotes", detail="No live quotes could be fetched")
                    return self._save(st)
                # ---- brief critical section: read HEAD, edit, verify, swap
                with self.versions.write_lock:
                    data = self.versions.head_bytes()
                    if price_by_name:
                        new, res = self.dao.update_prices(data, price_by_name, as_of)
                    else:
                        new, res = None, None
                    gold_new = self.gold.refresh_sheet_values(new or data) if self.gold is not None else None
                    final = gold_new or new
                    if final is not None:
                        parts = []
                        if res is not None and res.changed:
                            parts.append(f"market rates for {len(res.updated_rows)} row(s)")
                        if gold_new is not None:
                            parts.append("gold values")
                        self.versions.replace_head_content(final, note="Refreshed " + " and ".join(parts))
                if res is None:
                    res = PriceUpdateResult(False, [], [], [], [], None, None, [])
                st["gold_written"] = gold_new is not None
                st.update(
                    result="written" if (res.changed or gold_new is not None) else "unchanged",
                    rows_updated=len(res.updated_rows),
                    rows_unchanged=len(res.unchanged_rows),
                    skipped_formula_rows=res.skipped_formula_rows,
                    missing=res.missing,
                    valuation_date=as_of.isoformat() if as_of else None,
                    valuation_cell=res.valuation_cell,
                    duration_ms=int((time.time() - started) * 1000),
                )
                if res.changed or gold_new is not None:
                    st["last_write"] = time.time()
                else:
                    st["last_write"] = (self.repo.get(self.STATUS_KEY, {}) or {}).get("last_write")
            except Exception as e:  # never let the scheduler die
                log.exception("price sync failed")
                st.update(result="error", detail=str(e))
            return self._save(st)

    def _save(self, st: dict) -> dict:
        prev = self.repo.get(self.STATUS_KEY, {}) or {}
        if "last_write" not in st and prev.get("last_write"):
            st["last_write"] = prev["last_write"]
        self.repo.put(self.STATUS_KEY, st)
        return self.status()

    # ---------------------------------------------------------------- scheduler
    def trigger(self) -> None:
        """Run soon (e.g. right after an import added rows with the trade price as rate)."""
        self._wake.set()

    def start(self, initial_delay: float = 3.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop():
            if self._stop.wait(initial_delay):
                return
            reason = "startup"
            while not self._stop.is_set():
                self.run_once(reason)
                self._wake.wait(self.interval_minutes() * 60)
                reason = "triggered" if self._wake.is_set() else "scheduled"
                self._wake.clear()

        self._thread = threading.Thread(target=loop, name="price-sync", daemon=True)
        self._thread.start()
        log.info("Price sync started (every %s min)", self.interval_minutes())

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
