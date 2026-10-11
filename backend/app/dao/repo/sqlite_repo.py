"""SQLite DAO for application metadata (NOT portfolio data - the workbook is the source of
truth). Holds version metadata, import staging, transaction ledger, settings, aliases and
caches. All SQL lives in this module."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    message TEXT NOT NULL,
    source TEXT NOT NULL,
    parent_id INTEGER,
    batch_id TEXT,
    filename TEXT,
    stats_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS secrets (
    key TEXT PRIMARY KEY,
    value BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS securities (
    isin TEXT PRIMARY KEY,
    sheet_name TEXT NOT NULL,
    broker_name TEXT,
    symbol TEXT,
    sector TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS symbols (
    sheet_name TEXT PRIMARY KEY,
    symbol TEXT,
    source TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS import_batches (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    status TEXT NOT NULL,
    base_version_id INTEGER,
    result_version_id INTEGER,
    data_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    batch_id TEXT,
    version_id INTEGER,
    broker TEXT,
    note_no TEXT,
    trade_date TEXT,
    isin TEXT,
    sheet_name TEXT,
    side TEXT,
    qty REAL,
    rate REAL,
    gross REAL,
    brokerage REAL,
    gst REAL,
    stt REAL,
    other_levies REAL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    broker TEXT NOT NULL,
    note_no TEXT NOT NULL,
    trade_date TEXT,
    batch_id TEXT,
    charges_json TEXT NOT NULL,
    net_payable REAL,
    created_at REAL NOT NULL,
    PRIMARY KEY (broker, note_no)
);
CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


class Repository:
    def __init__(self, path: Path):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, args))

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, args)
            return cur.lastrowid

    # ------------------------------------------------------------------ kv
    def get(self, key: str, default: Any = None) -> Any:
        rows = self._q("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def put(self, key: str, value: Any) -> None:
        self._x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)))

    def get_secret(self, key: str) -> bytes | None:
        rows = self._q("SELECT value FROM secrets WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def put_secret(self, key: str, value: bytes | None) -> None:
        if value is None:
            self._x("DELETE FROM secrets WHERE key=?", (key,))
        else:
            self._x("INSERT INTO secrets(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value))

    # ------------------------------------------------------------ versions
    def add_version(self, *, sha256: str, size: int, message: str, source: str, parent_id: int | None,
                    batch_id: str | None = None, filename: str | None = None, stats: dict | None = None) -> int:
        return self._x(
            "INSERT INTO versions(created_at,sha256,size,message,source,parent_id,batch_id,filename,stats_json)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (time.time(), sha256, size, message, source, parent_id, batch_id, filename, json.dumps(stats or {})),
        )

    def versions(self) -> list[dict]:
        return [self._version(r) for r in self._q("SELECT * FROM versions ORDER BY id DESC")]

    def version(self, vid: int) -> dict | None:
        rows = self._q("SELECT * FROM versions WHERE id=?", (vid,))
        return self._version(rows[0]) if rows else None

    @staticmethod
    def _version(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["stats"] = json.loads(d.pop("stats_json") or "{}")
        return d

    # ---------------------------------------------------------- securities
    def security_by_isin(self, isin: str) -> dict | None:
        rows = self._q("SELECT * FROM securities WHERE isin=?", (isin,))
        return dict(rows[0]) if rows else None

    def securities(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM securities ORDER BY sheet_name")]

    def upsert_security(self, isin: str, sheet_name: str, broker_name: str | None, sector: str | None,
                        symbol: str | None = None) -> None:
        self._x(
            "INSERT INTO securities(isin,sheet_name,broker_name,symbol,sector,updated_at) VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(isin) DO UPDATE SET sheet_name=excluded.sheet_name, broker_name=excluded.broker_name,"
            " sector=COALESCE(excluded.sector, securities.sector),"
            " symbol=COALESCE(excluded.symbol, securities.symbol), updated_at=excluded.updated_at",
            (isin, sheet_name, broker_name, symbol, sector, time.time()),
        )

    def symbols(self) -> dict[str, dict]:
        return {r["sheet_name"]: dict(r) for r in self._q("SELECT * FROM symbols")}

    def set_symbol(self, sheet_name: str, symbol: str | None, source: str) -> None:
        self._x(
            "INSERT INTO symbols(sheet_name,symbol,source,updated_at) VALUES(?,?,?,?)"
            " ON CONFLICT(sheet_name) DO UPDATE SET symbol=excluded.symbol, source=excluded.source,"
            " updated_at=excluded.updated_at",
            (sheet_name, symbol, source, time.time()),
        )

    # ------------------------------------------------------------- imports
    def save_batch(self, batch_id: str, status: str, base_version_id: int | None, data: dict,
                   result_version_id: int | None = None) -> None:
        self._x(
            "INSERT INTO import_batches(id,created_at,status,base_version_id,result_version_id,data_json)"
            " VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,"
            " base_version_id=excluded.base_version_id, result_version_id=excluded.result_version_id,"
            " data_json=excluded.data_json",
            (batch_id, time.time(), status, base_version_id, result_version_id, json.dumps(data, default=str)),
        )

    def batch(self, batch_id: str) -> dict | None:
        rows = self._q("SELECT * FROM import_batches WHERE id=?", (batch_id,))
        if not rows:
            return None
        d = dict(rows[0])
        d["data"] = json.loads(d.pop("data_json"))
        return d

    def batches(self, limit: int = 50) -> list[dict]:
        out = []
        for r in self._q("SELECT * FROM import_batches ORDER BY created_at DESC LIMIT ?", (limit,)):
            d = dict(r)
            data = json.loads(d.pop("data_json"))
            d["summary"] = data.get("summary", {})
            d["files"] = [{"filename": f.get("filename"), "status": f.get("status")} for f in data.get("files", [])]
            out.append(d)
        return out

    # -------------------------------------------------------------- ledger
    def add_ledger(self, rows: list[dict]) -> None:
        with self.tx() as c:
            for r in rows:
                c.execute(
                    "INSERT INTO ledger(created_at,batch_id,version_id,broker,note_no,trade_date,isin,sheet_name,side,"
                    "qty,rate,gross,brokerage,gst,stt,other_levies,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (time.time(), r.get("batch_id"), r.get("version_id"), r.get("broker"), r.get("note_no"),
                     r.get("trade_date"), r.get("isin"), r.get("sheet_name"), r.get("side"), r.get("qty"),
                     r.get("rate"), r.get("gross"), r.get("brokerage"), r.get("gst"), r.get("stt"),
                     r.get("other_levies"), r["status"]),
                )

    def ledger(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM ledger ORDER BY trade_date DESC, id DESC")]

    def upsert_note(self, broker: str, note_no: str, trade_date: str, batch_id: str, charges: dict,
                    net_payable: float) -> None:
        self._x(
            "INSERT INTO notes(broker,note_no,trade_date,batch_id,charges_json,net_payable,created_at)"
            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(broker,note_no) DO UPDATE SET charges_json=excluded.charges_json,"
            " net_payable=excluded.net_payable",
            (broker, note_no, trade_date, batch_id, json.dumps(charges), net_payable, time.time()),
        )

    def notes(self) -> list[dict]:
        out = []
        for r in self._q("SELECT * FROM notes ORDER BY trade_date DESC"):
            d = dict(r)
            d["charges"] = json.loads(d.pop("charges_json"))
            out.append(d)
        return out

    # --------------------------------------------------------------- cache
    def cache_get(self, key: str, max_age: float) -> Any:
        rows = self._q("SELECT value, fetched_at FROM cache WHERE key=?", (key,))
        if rows and time.time() - rows[0]["fetched_at"] <= max_age:
            return json.loads(rows[0]["value"])
        return None

    def cache_put(self, key: str, value: Any) -> None:
        self._x("INSERT INTO cache(key,value,fetched_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET"
                " value=excluded.value, fetched_at=excluded.fetched_at", (key, json.dumps(value), time.time()))

    # --------------------------------------------------------------- audit
    def audit(self, action: str, detail: dict) -> None:
        self._x("INSERT INTO audit(ts,action,detail) VALUES(?,?,?)", (time.time(), action, json.dumps(detail, default=str)))

    def audit_log(self, limit: int = 200) -> list[dict]:
        out = []
        for r in self._q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)):
            d = dict(r)
            d["detail"] = json.loads(d["detail"])
            out.append(d)
        return out
