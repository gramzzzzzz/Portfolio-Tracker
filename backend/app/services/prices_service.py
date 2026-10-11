"""Market prices with caching; maps sheet security names to exchange symbols."""
from __future__ import annotations

import datetime as dt
import logging
import re

from app.dao.base import PriceProvider
from app.dao.repo.sqlite_repo import Repository

log = logging.getLogger(__name__)

QUOTE_TTL = 5 * 60
HIST_TTL = 12 * 3600
RESOLVE_TTL = 30 * 86400


class PriceService:
    def __init__(self, repo: Repository, provider: PriceProvider | None):
        self.repo = repo
        self.provider = provider

    def resolve(self, query: str) -> list[dict]:
        if not self.provider or not query:
            return []
        key = f"resolve:{query.upper()}"
        cached = self.repo.cache_get(key, RESOLVE_TTL)
        if cached is not None:
            return cached
        res = self.provider.resolve(query)
        if res:
            self.repo.cache_put(key, res)
        return res

    def symbol_map(self, names: list[str], codes: dict[str, str | None], auto_resolve: bool) -> dict[str, str | None]:
        """sheet name -> symbol. Uses manual overrides, symbols learned from imports, then
        (optionally) a one-time lookup by the Code column / name."""
        known = self.repo.symbols()
        by_isin = {s["sheet_name"]: s.get("symbol") for s in self.repo.securities() if s.get("symbol")}
        out = {}
        for n in names:
            if n in known:
                out[n] = known[n].get("symbol")
                continue
            if by_isin.get(n):
                out[n] = by_isin[n]
                continue
            out[n] = None
            if auto_resolve and self.provider:
                q = (codes.get(n) or "").strip()
                cands = []
                if q and re.fullmatch(r"[A-Za-z0-9&\-]{2,20}", q):
                    cands = [c for c in self.resolve(q) if str(c["symbol"]).upper().startswith(q.upper())]
                if not cands:
                    cands = self.resolve(n)
                sym = cands[0]["symbol"] if cands else None
                self.repo.set_symbol(n, sym, "auto")
                out[n] = sym
        return out

    def quotes(self, symbols: list[str]) -> dict[str, dict]:
        if not self.provider:
            return {}
        out, missing = {}, []
        for s in {x for x in symbols if x}:
            c = self.repo.cache_get(f"quote:{s}", QUOTE_TTL)
            if c:
                out[s] = c
            else:
                missing.append(s)
        if missing:
            fresh = self.provider.quotes(missing)
            for s, q in fresh.items():
                self.repo.cache_put(f"quote:{s}", q)
                out[s] = q
        return out

    def history(self, symbol: str, start: dt.date) -> list[tuple[dt.date, float]]:
        if not self.provider:
            return []
        key = f"hist:{symbol}:{start.isoformat()}"
        c = self.repo.cache_get(key, HIST_TTL)
        if c is not None:
            return [(dt.date.fromisoformat(d), p) for d, p in c]
        h = self.provider.history(symbol, start)
        if h:
            self.repo.cache_put(key, [(d.isoformat(), p) for d, p in h])
        return h
