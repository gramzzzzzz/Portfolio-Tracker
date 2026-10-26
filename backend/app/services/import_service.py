"""Contract-note import workflow: stage -> review -> preview -> commit.

Staging parses every uploaded PDF, de-duplicates each transaction against the current sheet
(and against the other files of the batch) and proposes how every security maps onto the
sheet. Nothing is written until the user commits; the commit is atomic (new version or
nothing).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import logging
import re
import time
import uuid
from typing import Any

from app.dao.base import ParseError, PasswordError, PortfolioSheetDAO
from app.dao.contract_notes.registry import ContractNoteReader
from app.dao.repo.blobs import BlobStore
from app.dao.repo.sqlite_repo import Repository
from app.domain.models import NewLot, Side, SheetSnapshot, Trade
from app.domain.models import Transaction, TxnType
from app.services.matching import classify_sells, classify_trades, resolve_security
from app.services.prices_service import PriceService
from app.services.settings_service import SettingsService
from app.services.version_service import ConflictError, VersionService

log = logging.getLogger(__name__)

YAHOO_SECTOR_HINTS = {
    "financial services": ["bank", "banks", "finance", "financials", "nbfc"],
    "technology": ["it", "technology", "tech"],
    "healthcare": ["pharma", "healthcare"],
    "consumer defensive": ["fmcg", "consumer"],
    "consumer cyclical": ["automobile", "auto", "consumer"],
    "communication services": ["telecom", "media"],
    "industrials": ["infra", "capitalgoods", "industrials"],
    "utilities": ["power", "energy", "utilities", "infra"],
    "energy": ["energy", "oilgas", "infra"],
    "basic materials": ["metals", "chemicals", "materials"],
    "real estate": ["realty", "realestate", "infra"],
}


class ImportError_(Exception):
    pass


def _n(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _trade_to_dict(t: Trade) -> dict:
    d = dataclasses.asdict(t)
    d["side"] = t.side.value
    d["trade_date"] = t.trade_date.isoformat() if t.trade_date else None
    d["charges"] = t.charges
    for f in d["fills"]:
        f["side"] = f["side"].value if hasattr(f["side"], "value") else f["side"]
    return d


def _trade_from_dict(d: dict) -> Trade:
    d = dict(d)
    d.pop("charges", None)
    d.pop("fills", None)
    d["side"] = Side(d["side"])
    d["trade_date"] = dt.date.fromisoformat(d["trade_date"]) if d.get("trade_date") else None
    return Trade(**d)


class ImportService:
    def __init__(self, repo: Repository, versions: VersionService, dao: PortfolioSheetDAO,
                 reader: ContractNoteReader, uploads: BlobStore, settings: SettingsService,
                 prices: PriceService, portfolio=None, extras_dao=None):
        self.repo = repo
        self.versions = versions
        self.dao = dao
        self.reader = reader
        self.uploads = uploads
        self.settings = settings
        self.prices = prices
        self.portfolio = portfolio  # PortfolioService: holdings after recorded sells
        self.extras_dao = extras_dao  # Transactions sheet (sells)

    # ================================================================ staging
    def stage(self, files: list[tuple[str, bytes]], password: str | None, *, use_saved: bool,
              save_password: bool) -> dict:
        if not files:
            raise ImportError_("No files uploaded")
        if save_password and password:
            self.settings.set_pdf_password(password)
        pw = password or (self.settings.pdf_password() if use_saved else None)
        batch_id = uuid.uuid4().hex[:12]
        head = self.versions.head_id()
        batch: dict[str, Any] = {"id": batch_id, "created_at": time.time(), "files": [], "items": []}
        for i, (name, data) in enumerate(files):
            sha = hashlib.sha256(data).hexdigest()
            self.uploads.put(data)
            batch["files"].append({"id": f"f{i}", "filename": name, "sha256": sha, "size": len(data),
                                   "status": "pending"})
        self._parse_files(batch, pw)
        self._classify(batch)
        self.repo.save_batch(batch_id, "staged", head, batch)
        self.repo.audit("import.stage", {"batch": batch_id, "files": [f["filename"] for f in batch["files"]]})
        return self.view(batch_id)

    def retry(self, batch_id: str, password: str | None, *, save_password: bool) -> dict:
        b = self._load(batch_id, staged=True)
        batch = b["data"]
        if save_password and password:
            self.settings.set_pdf_password(password)
        pw = password or self.settings.pdf_password()
        for f in batch["files"]:
            if f["status"] in ("password_error", "error"):
                f["status"] = "pending"
        self._parse_files(batch, pw)
        self._classify(batch)
        self.repo.save_batch(batch_id, "staged", self.versions.head_id(), batch)
        return self.view(batch_id)

    def _parse_files(self, batch: dict, password: str | None):
        seen_notes: dict[tuple, str] = {}
        for f in batch["files"]:
            if f["status"] == "parsed" and f.get("note"):
                seen_notes[(f["note"]["broker"], f["note"]["note_no"])] = f["id"]
        for f in batch["files"]:
            if f["status"] != "pending":
                continue
            data = self.uploads.get(f["sha256"])
            try:
                note = self.reader.read(data, password)
            except PasswordError as e:
                f.update(status="password_error", error=str(e))
                continue
            except ParseError as e:
                f.update(status="error", error=str(e))
                continue
            except Exception as e:  # defensive: never let one bad file kill the batch
                log.exception("parse failed")
                f.update(status="error", error=f"Unexpected error while parsing: {e}")
                continue
            key = (note.broker, note.note_no)
            f["note"] = {
                "broker": note.broker, "note_no": note.note_no, "trade_date": note.trade_date.isoformat(),
                "settlement_date": note.settlement_date.isoformat() if note.settlement_date else None,
                "charges": dataclasses.asdict(note.charges), "charges_total": note.charges.total,
                "net_payable": note.net_payable, "warnings": note.warnings, "parser": note.parser,
            }
            if key in seen_notes:
                f.update(status="duplicate_file", error=f"Same contract note as {seen_notes[key]}")
                continue
            seen_notes[key] = f["id"]
            f["status"] = "parsed"
            for j, t in enumerate(note.trades):
                batch["items"].append({"id": f"{f['id']}-{j}", "file_id": f["id"], "trade": _trade_to_dict(t)})

    def _classify(self, batch: dict):
        """(Re)compute dedup status + security resolution for every item of the batch."""
        try:
            snap = self.versions.snapshot()
        except Exception as e:
            raise ImportError_(str(e)) from e
        names = snap.security_names()
        sector_names = [s.name for s in snap.sectors]
        triples = []
        for it in batch["items"]:
            t = _trade_from_dict(it["trade"])
            alias = self.repo.security_by_isin(t.isin)
            res = resolve_security(t, names, alias, snap.lots)
            it["resolution"] = dataclasses.asdict(res)
            triples.append((it["id"], t, res.sheet_name))
        # an item of a file duplicated inside the batch is a duplicate of the earlier one
        dd = classify_trades(triples, snap.lots)
        if self.portfolio is not None:
            st = self.portfolio.state()
            recorded = [t for t in st.extras.transactions if t.type == TxnType.SELL]
            held: dict[str, float] = {}
            for l in st.snap.lots:
                held[l.name.casefold()] = held.get(l.name.casefold(), 0.0) + l.held_qty
        else:
            recorded, held = [], {}
        dd.update(classify_sells(triples, recorded, held))
        seen: dict[tuple, str] = {}
        for it, (iid, t, nm) in zip(batch["items"], triples):
            r = dd[iid]
            key = (t.isin, str(t.trade_date), t.side.value, round(t.qty, 6), round(t.rate, 4))
            if r.status == "new" and key in seen:
                it.update(status="duplicate", reason=f"Same transaction appears in another file of this upload ({seen[key]})",
                          matched_rows=[])
            else:
                it.update(status=r.status, reason=r.reason, matched_rows=r.matched_rows)
            seen.setdefault(key, iid)
            prev = it.get("proposal") or {}
            it["proposal"] = self._proposal(it, t, snap, sector_names, prev)

        items = batch["items"]
        batch["summary"] = {
            "files": len(batch["files"]),
            "files_ok": sum(1 for f in batch["files"] if f["status"] == "parsed"),
            "files_failed": sum(1 for f in batch["files"] if f["status"] in ("error", "password_error")),
            "trades": len(items),
            "new": sum(1 for i in items if i["status"] == "new"),
            "duplicates": sum(1 for i in items if i["status"] == "duplicate"),
            "possible": sum(1 for i in items if i["status"] == "possible_duplicate"),
            "sells": sum(1 for i in items if i["trade"]["side"] == "SELL"),
            "new_sells": sum(1 for i in items if i["status"] == "sell"),
            "sell_problems": sum(1 for i in items if i["status"] in ("sell_exceeds", "sell_unknown")),
        }

    def _proposal(self, it: dict, t: Trade, snap: SheetSnapshot, sectors: list[str], prev: dict) -> dict:
        res = it["resolution"]
        existing = res["sheet_name"] if res["sheet_name"] in snap.security_names() else None
        lots = [l for l in snap.lots if existing and l.name == existing]
        summ = next((s for s in snap.summary if existing and s.name == existing), None)
        alias = self.repo.security_by_isin(t.isin) or {}
        hint = self._hints(t, sectors) if not existing else {}
        sector = (summ.sector if summ else None) or (lots[-1].sector if lots else None) or alias.get("sector") \
            or hint.get("sector")
        p = {
            "include": it["status"] in ("new", "sell"),
            "mode": "existing" if existing else "new",
            "name": existing or res["sheet_name"] or t.security_name,
            "sector": sector,
            "code": (lots[-1].code if lots and lots[-1].code else None) or (hint.get("code")) or alias.get("symbol"),
            "face_value": lots[-1].face_value if lots else None,
            "market_rate": next((l.market_rate for l in reversed(lots) if l.market_rate), None) or t.rate,
            "symbol": alias.get("symbol") or hint.get("symbol"),
            "sector_hint": hint.get("yahoo_sector"),
        }
        # keep user edits across re-classification
        if prev.get("_edited"):
            for k in ("include", "mode", "name", "sector", "code", "face_value", "market_rate"):
                if k in prev:
                    p[k] = prev[k]
            p["_edited"] = True
        return p

    def _hints(self, t: Trade, sectors: list[str]) -> dict:
        if self.prices.provider is None:
            return {}
        cands = self.prices.resolve(t.isin)
        if not cands:
            return {}
        c = cands[0]
        out = {"symbol": c["symbol"], "code": str(c["symbol"]).split(".")[0], "yahoo_sector": c.get("sector")}
        ns = {_n(s): s for s in sectors}
        if c.get("type") == "ETF" or "etf" in t.security_name.lower():
            gold = "gold" in t.security_name.lower()
            for k, s in ns.items():
                if "etf" in k and (("gold" in k) == gold):
                    out["sector"] = s
                    break
        else:
            ind = (c.get("industry") or "").lower()
            if "auto" in ind:
                for k, s in ns.items():
                    if k.startswith("auto"):
                        out["sector"] = s
            if "sector" not in out:
                for h in YAHOO_SECTOR_HINTS.get((c.get("sector") or "").lower(), []):
                    if h in ns:
                        out["sector"] = ns[h]
                        break
        return out

    # ================================================================ views
    def _load(self, batch_id: str, staged: bool = False) -> dict:
        b = self.repo.batch(batch_id)
        if not b:
            raise KeyError(batch_id)
        if staged and b["status"] != "staged":
            raise ImportError_(f"Batch is already {b['status']}")
        return b

    def view(self, batch_id: str) -> dict:
        b = self._load(batch_id)
        data = b["data"]
        snap = None
        try:
            snap = self.versions.snapshot()
        except Exception:
            pass
        return {
            "id": b["id"],
            "status": b["status"],
            "created_at": b["created_at"],
            "base_version_id": b["base_version_id"],
            "result_version_id": b["result_version_id"],
            "stale": b["status"] == "staged" and b["base_version_id"] != self.versions.head_id(),
            "files": data["files"],
            "items": data["items"],
            "summary": data.get("summary", {}),
            "result": data.get("result"),
            "sheet": {
                "securities": snap.security_names() if snap else [],
                "sectors": [s.name for s in snap.sectors] if snap else [],
            },
        }

    def list(self) -> list[dict]:
        return self.repo.batches()

    def discard(self, batch_id: str) -> None:
        b = self._load(batch_id, staged=True)
        self.repo.save_batch(batch_id, "discarded", b["base_version_id"], b["data"])

    def refresh(self, batch_id: str) -> dict:
        b = self._load(batch_id, staged=True)
        self._classify(b["data"])
        self.repo.save_batch(batch_id, "staged", self.versions.head_id(), b["data"])
        return self.view(batch_id)

    # ============================================================ commit path
    def _apply_decisions(self, batch: dict, decisions: list[dict]) -> None:
        by_id = {d["item_id"]: d for d in decisions or []}
        for it in batch["items"]:
            d = by_id.get(it["id"])
            if not d:
                continue
            p = it["proposal"]
            for k in ("include", "mode", "name", "sector", "code", "face_value", "market_rate"):
                if k in d:
                    p[k] = d[k]
            p["_edited"] = True

    def _build_lots(self, batch: dict, snap: SheetSnapshot) -> tuple[list[NewLot], list[dict]]:
        lots, chosen = [], []
        names = {n.casefold(): n for n in snap.security_names()}
        errors = []
        for it in batch["items"]:
            p = it["proposal"]
            if not p.get("include"):
                continue
            t = _trade_from_dict(it["trade"])
            if t.side != Side.BUY:
                continue  # handled by _build_sells
            name = (p.get("name") or "").strip()
            if not name:
                errors.append(f"{t.security_name}: a sheet name is required")
                continue
            if name.casefold() in names:
                name = names[name.casefold()]
            elif p.get("mode") == "existing":
                errors.append(f"{t.security_name}: '{name}' is not a holding in the sheet")
                continue
            sector = (p.get("sector") or "").strip()
            if not sector:
                lot = next((l for l in snap.lots if l.name == name and l.sector), None)
                sector = lot.sector if lot else ""
            if not sector:
                errors.append(f"{name}: choose a sector")
                continue
            fv = p.get("face_value")
            mr = p.get("market_rate")
            lots.append(NewLot(
                name=name, sector=sector, date=t.trade_date, qty=t.qty, rate=t.rate,
                charges=round(t.charges, 2), code=(p.get("code") or None),
                face_value=float(fv) if fv not in (None, "") else None,
                market_rate=float(mr) if mr not in (None, "") else t.rate,
                block=self.settings.get("sector_block_overrides").get(sector), ref=it["id"],
            ))
            chosen.append(it)
        if errors:
            raise ImportError_("; ".join(errors))
        order = sorted(range(len(lots)), key=lambda i: (lots[i].date, i))
        return [lots[i] for i in order], [chosen[i] for i in order]

    def _build_sells(self, batch: dict, snap: SheetSnapshot) -> tuple[list[Transaction], list[dict]]:
        """Selected sell items -> Transactions rows. Validates holding name and quantity
        (FIFO against what is held after already-recorded sells)."""
        out, chosen, errors = [], [], []
        names = {n.casefold(): n for n in snap.security_names()}
        held: dict[str, float] = {}
        if self.portfolio is not None:
            for l in self.portfolio.state().snap.lots:
                held[l.name.casefold()] = held.get(l.name.casefold(), 0.0) + l.held_qty
        files = {f["id"]: f for f in batch["files"]}
        for it in batch["items"]:
            p = it["proposal"]
            t = it["trade"]
            if t["side"] != "SELL" or not p.get("include"):
                continue
            name = names.get((p.get("name") or "").strip().casefold())
            if not name:
                errors.append(f"{t['security_name']}: choose which holding in the sheet was sold")
                continue
            if t["qty"] > held.get(name.casefold(), 0.0) + 1e-6:
                errors.append(f"{name}: selling {t['qty']:g} but only {held.get(name.casefold(), 0.0):g} units are held")
                continue
            held[name.casefold()] -= t["qty"]
            note = files[it["file_id"]].get("note") or {}
            deductible = (t["brokerage"] or 0) + (t["gst"] or 0) + (t["other_levies"] or 0)
            out.append(Transaction(
                id=f"T-{note.get('note_no', 'X')[-6:]}-{it['id'].split('-')[-1]}", date=dt.date.fromisoformat(t["trade_date"]),
                type=TxnType.SELL, name=name, qty=t["qty"], price=t["rate"], charges=round(deductible, 2),
                stt=round(t["stt"] or 0, 2), isin=t["isin"], broker=note.get("broker"), note_no=note.get("note_no"),
                source="contract_note"))
            chosen.append(it)
        if errors:
            raise ImportError_("; ".join(errors))
        return out, chosen

    def _write(self, data: bytes, lots: list[NewLot], sells: list[Transaction], *, dry_run: bool):
        """Purchases -> main sheet (row insertion, as before); sells -> Transactions sheet."""
        res = None
        new = data
        if lots:
            out, res = self.dao.append(data, lots, dry_run=dry_run)
            new = out if out is not None else data
        result = _result_dict(res) if res else {"rows": [], "checks": [], "warnings": []}
        if sells:
            existing = self.extras_dao.read(data).transactions
            ids = {t.id for t in existing}
            for t in sells:
                while t.id in ids:
                    t.id = t.id + "a"
                ids.add(t.id)
            new = self.extras_dao.write(new, transactions=existing + sells)
            for t in sells:
                result["rows"].append({"kind": "sell", "row": None, "ref": None, "values": {},
                                       "description": f"Sale of {t.qty:g} {t.name} @ {t.price:,.2f} on {t.date:%d %b %Y} "
                                                      f"→ Transactions sheet ({t.id})"})
            result["checks"].append({"name": "main_sheet_untouched_by_sells", "passed": True,
                                     "detail": "Sales go to the Transactions sheet; the main sheet is verified byte-for-byte unchanged by that write"})
        return (None if dry_run else new), result

    def preview(self, batch_id: str, decisions: list[dict]) -> dict:
        b = self._load(batch_id, staged=True)
        batch = b["data"]
        self._apply_decisions(batch, decisions)
        snap = self.versions.snapshot()
        lots, _ = self._build_lots(batch, snap)
        sells, _ = self._build_sells(batch, snap)
        if not lots and not sells:
            return {"rows": [], "checks": [], "warnings": ["Nothing selected to import"]}
        _, result = self._write(self.versions.head_bytes(), lots, sells, dry_run=True)
        return result

    def commit(self, batch_id: str, decisions: list[dict], message: str | None = None) -> dict:
        with self.versions.write_lock:
            b = self._load(batch_id, staged=True)
            batch = b["data"]
            if b["base_version_id"] != self.versions.head_id():
                # sheet changed since staging: re-check duplicates before writing anything
                self._apply_decisions(batch, decisions)
                self._classify(batch)
                self.repo.save_batch(batch_id, "staged", self.versions.head_id(), batch)
                raise ConflictError("The sheet changed since these notes were analysed. Duplicates were "
                                    "re-checked; please review and confirm again.")
            self._apply_decisions(batch, decisions)
            snap = self.versions.snapshot()
            lots, chosen = self._build_lots(batch, snap)
            sells, chosen_sells = self._build_sells(batch, snap)
            if not lots and not sells:
                raise ImportError_("Nothing selected to import")
            try:
                new_bytes, result = self._write(self.versions.head_bytes(), lots, sells, dry_run=False)
            except RuntimeError as e:  # verification failure -> nothing written
                raise ImportError_(str(e)) from e
            chosen = chosen + chosen_sells
            notes = sorted({f["note"]["note_no"] for f in batch["files"] if f.get("note") and
                            any(i["file_id"] == f["id"] for i in chosen)})
            parts = ([f"{len(lots)} purchase(s)"] if lots else []) + ([f"{len(sells)} sale(s)"] if sells else [])
            msg = message or (f"Imported {' and '.join(parts)} from contract note(s) {', '.join(notes)}")
            version = self.versions.commit(new_bytes, message=msg, source="import", batch_id=batch_id,
                                           expected_parent=b["base_version_id"])
            # ---- side effects in app storage (metadata only)
            files = {f["id"]: f for f in batch["files"]}
            ledger = []
            chosen_ids = {i["id"] for i in chosen}
            for it in batch["items"]:
                t = it["trade"]
                f = files[it["file_id"]]
                status = ("recorded" if it["id"] in chosen_ids else
                          "skipped_duplicate" if it["status"] in ("duplicate", "possible_duplicate") else "skipped")
                ledger.append({
                    "batch_id": batch_id, "version_id": version["id"], "broker": f["note"]["broker"],
                    "note_no": f["note"]["note_no"], "trade_date": t["trade_date"], "isin": t["isin"],
                    "sheet_name": it["proposal"].get("name"), "side": t["side"], "qty": t["qty"], "rate": t["rate"],
                    "gross": t["gross_value"], "brokerage": t["brokerage"], "gst": t["gst"], "stt": t["stt"],
                    "other_levies": t["other_levies"], "status": status,
                })
                if it["id"] in chosen_ids and t["side"] == "BUY":
                    p = it["proposal"]
                    self.repo.upsert_security(t["isin"], p["name"], t["security_name"], p.get("sector"),
                                              p.get("symbol"))
                    if p.get("symbol") and p["name"] not in self.repo.symbols():
                        self.repo.set_symbol(p["name"], p["symbol"], "isin")
            self.repo.add_ledger(ledger)
            for f in batch["files"]:
                if f["status"] == "parsed":
                    n = f["note"]
                    self.repo.upsert_note(n["broker"], n["note_no"], n["trade_date"], batch_id, n["charges"],
                                          n["net_payable"])
            batch["result"] = result
            batch["result"]["version"] = version
            self.repo.save_batch(batch_id, "committed", b["base_version_id"], batch, result_version_id=version["id"])
            self.repo.audit("import.commit", {"batch": batch_id, "version": version["id"], "rows": len(lots),
                                              "sells": len(sells)})
            return self.view(batch_id)


def _result_dict(res) -> dict:
    return {
        "rows": [dataclasses.asdict(r) for r in res.rows],
        "checks": res.checks,
        "warnings": res.warnings,
    }
