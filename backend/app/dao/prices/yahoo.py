"""Market data DAO backed by Yahoo Finance public chart/search endpoints.

Optional: the application works fully offline using the market rates stored in the sheet.
Network failures are swallowed (logged) and simply result in missing quotes.
"""
from __future__ import annotations

import datetime as dt
import logging

import httpx

log = logging.getLogger(__name__)

UA = {"User-Agent": "Mozilla/5.0 (portfolio-tracker)"}


class YahooPriceProvider:
    name = "yahoo"

    def __init__(self, timeout: float = 6.0):
        self.client = httpx.Client(timeout=timeout, headers=UA, follow_redirects=True)

    def _chart(self, symbol: str, params: dict) -> dict | None:
        try:
            r = self.client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}", params=params)
            if r.status_code != 200:
                return None
            res = (r.json().get("chart") or {}).get("result") or []
            return res[0] if res else None
        except (httpx.HTTPError, ValueError) as e:
            log.warning("yahoo chart %s failed: %s", symbol, e)
            return None

    def quotes(self, symbols: list[str]) -> dict[str, dict]:
        out = {}
        for s in symbols:
            res = self._chart(s, {"range": "5d", "interval": "1d"})
            if not res:
                continue
            m = res.get("meta", {})
            price = m.get("regularMarketPrice")
            prev = m.get("chartPreviousClose") or m.get("previousClose")
            closes = [c for c in ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close", []) if c]
            if len(closes) >= 2:
                prev = closes[-2]
            if price is None:
                continue
            out[s] = {
                "price": float(price),
                "prev_close": float(prev) if prev else None,
                "change_pct": (float(price) / float(prev) - 1) * 100 if prev else None,
                "high52": m.get("fiftyTwoWeekHigh"),
                "low52": m.get("fiftyTwoWeekLow"),
                "time": m.get("regularMarketTime"),
                "currency": m.get("currency"),
            }
        return out

    def history(self, symbol: str, start: dt.date) -> list[tuple[dt.date, float]]:
        p1 = int(dt.datetime.combine(start - dt.timedelta(days=7), dt.time()).timestamp())
        p2 = int(dt.datetime.now().timestamp())
        res = self._chart(symbol, {"period1": p1, "period2": p2, "interval": "1d"})
        if not res:
            return []
        ts = res.get("timestamp") or []
        closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close", [])
        out = []
        for t, c in zip(ts, closes):
            if c is not None:
                out.append((dt.datetime.fromtimestamp(t, dt.timezone(dt.timedelta(hours=5, minutes=30))).date(), float(c)))
        return out

    def resolve(self, query: str) -> list[dict]:
        try:
            r = self.client.get(
                "https://query2.finance.yahoo.com/v1/finance/search",
                params={"q": query, "quotesCount": 6, "newsCount": 0},
            )
            if r.status_code != 200:
                return []
            out = []
            for q in r.json().get("quotes", []):
                if q.get("exchange") in ("NSI", "BSE", "BOM") or str(q.get("symbol", "")).endswith((".NS", ".BO")):
                    out.append({
                        "symbol": q.get("symbol"),
                        "name": q.get("longname") or q.get("shortname"),
                        "exchange": q.get("exchDisp"),
                        "type": q.get("quoteType"),
                        "sector": q.get("sectorDisp") or q.get("sector"),
                        "industry": q.get("industryDisp") or q.get("industry"),
                    })
            out.sort(key=lambda x: 0 if str(x["symbol"]).endswith(".NS") else 1)
            return out
        except (httpx.HTTPError, ValueError) as e:
            log.warning("yahoo search %s failed: %s", query, e)
            return []
