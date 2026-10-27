"""HTTP API (FastAPI). Thin layer: validation, auth, mapping service errors to HTTP."""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from contextlib import asynccontextmanager
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.api.auth import COOKIE, make_token, require_session, verify_token
from app.config import Config, load_config
from app.container import Container, build
from app.dao.base import LayoutError
from app.services.finance import fy_label
from app.services.import_service import ImportError_
from app.dao.mail.gmail import GmailError
from app.services.email_sync_service import EmailSyncError
from app.services.sheet_lock import LockTimeout
from app.services.version_service import ConflictError, NoSheetError

log = logging.getLogger("portfolio")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ------------------------------------------------------------------ schemas
class PasswordIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class Decision(BaseModel):
    item_id: str
    include: bool | None = None
    mode: str | None = Field(default=None, pattern="^(existing|new)$")
    name: str | None = Field(default=None, max_length=120)
    sector: str | None = Field(default=None, max_length=60)
    code: str | None = Field(default=None, max_length=40)
    face_value: float | None = None
    market_rate: float | None = None


class DecisionsIn(BaseModel):
    decisions: list[Decision] = []
    message: str | None = Field(default=None, max_length=300)


class RetryIn(BaseModel):
    password: str | None = Field(default=None, max_length=256)
    save_password: bool = False


class SymbolIn(BaseModel):
    symbol: str | None = Field(default=None, max_length=40)


class SaleIn(BaseModel):
    name: str = Field(max_length=120)
    units: float | None = Field(default=None, gt=0)
    amount: float | None = Field(default=None, gt=0)
    sale_date: dt.date | None = None
    price: float | None = Field(default=None, gt=0)


class RaiseCashIn(BaseModel):
    amount: float = Field(gt=0, le=1e10)
    exclude: list[str] = []


class WithdrawalIn(BaseModel):
    monthly_amount: float = Field(gt=0, le=1e9)
    months: int = Field(ge=1, le=600)
    annual_return_pct: float = Field(default=10.0, ge=-50, le=50)
    start: dt.date | None = None
    exclude: list[str] = []


class TxnIn(BaseModel):
    date: dt.date
    type: str = Field(pattern="^(SELL|BONUS|SPLIT)$")
    name: str = Field(min_length=1, max_length=120)
    qty: float = Field(default=0.0, ge=0)
    price: float | None = Field(default=None, ge=0)
    charges: float = Field(default=0.0, ge=0)
    stt: float = Field(default=0.0, ge=0)
    ratio: float | None = Field(default=None, gt=0)
    remarks: str | None = Field(default=None, max_length=300)

    def to_domain(self, txn_id: str | None = None):
        from app.domain.models import Transaction, TxnType
        return Transaction(id=txn_id or "", date=self.date, type=TxnType(self.type), name=self.name.strip(),
                           qty=self.qty, price=self.price, charges=self.charges, stt=self.stt, ratio=self.ratio,
                           remarks=self.remarks, source="manual")


class GoldIn(BaseModel):
    date: dt.date
    description: str = Field(min_length=1, max_length=120)
    form: str = Field(pattern="^(coin|bar|jewellery)$")
    purity: str = Field(min_length=1, max_length=12)
    weight_g: float = Field(gt=0, le=1e6)
    amount_paid: float = Field(gt=0, le=1e10)
    making_charges: float = Field(default=0.0, ge=0)
    gst_paid: float = Field(default=0.0, ge=0)
    seller: str | None = Field(default=None, max_length=120)
    sold_date: dt.date | None = None
    sold_amount: float | None = Field(default=None, ge=0)
    remarks: str | None = Field(default=None, max_length=300)

    def to_domain(self, item_id: str | None = None):
        from app.domain.models import GoldItem
        return GoldItem(id=item_id or "", **self.model_dump())


class EmailClientIn(BaseModel):
    client_id: str | None = Field(default=None, max_length=200)
    client_secret: str | None = Field(default=None, max_length=200)
    client_json: str | None = Field(default=None, max_length=10000)


class EmailCompleteIn(BaseModel):
    url: str = Field(min_length=10, max_length=4000)


class EmailSyncIn(BaseModel):
    query: str | None = Field(default=None, max_length=300)
    only_new: bool | None = None
    password: str | None = Field(default=None, max_length=256)
    save_password: bool = False


class RealisedIn(BaseModel):
    st_equity: float = 0.0
    lt_equity: float = 0.0
    st_slab: float = 0.0
    lt_other: float = 0.0


def create_app(config: Config | None = None, container: Container | None = None) -> FastAPI:
    config = config or load_config()
    c = container or build(config)
    @asynccontextmanager
    async def lifespan(_app):
        if config.price_sync_minutes > 0:
            c.price_sync.start()  # runs at startup, then every `price_sync_minutes`
        yield
        c.price_sync.stop()

    app = FastAPI(title="Portfolio Tracker", version="1.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json",
                  lifespan=lifespan)
    app.state.container = c
    max_bytes = config.max_upload_mb * 1024 * 1024

    @app.exception_handler(NoSheetError)
    async def _nosheet(_req, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc), "code": "no_sheet"})

    @app.exception_handler(LayoutError)
    async def _layout(_req, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "layout"})

    @app.exception_handler(ImportError_)
    async def _imp(_req, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "import"})

    @app.exception_handler(ConflictError)
    async def _conf(_req, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc), "code": "conflict"})

    @app.exception_handler(LockTimeout)
    async def _busy(_req, exc):
        return JSONResponse(status_code=503, content={"detail": str(exc), "code": "busy"})

    @app.exception_handler(EmailSyncError)
    async def _mail(_req, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "email"})

    @app.exception_handler(GmailError)
    async def _gmail(_req, exc):
        return JSONResponse(status_code=502, content={"detail": str(exc), "code": "gmail"})

    @app.exception_handler(KeyError)
    async def _nf(_req, exc):
        return JSONResponse(status_code=404, content={"detail": "Not found"})

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'"
        )
        return resp

    async def read_upload(f: UploadFile, kind: str) -> bytes:
        data = await f.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise HTTPException(413, f"{f.filename} is larger than {config.max_upload_mb} MB")
        if kind == "pdf" and not data.startswith(b"%PDF"):
            raise HTTPException(415, f"{f.filename} is not a PDF")
        if kind == "xlsx" and not data.startswith(b"PK"):
            raise HTTPException(415, f"{f.filename} is not an .xlsx workbook")
        return data

    # ================================================================ public
    public = APIRouter(prefix="/api")
    failures: dict[str, deque] = defaultdict(deque)

    @public.get("/health")
    def health():
        return {"ok": True, "sheet": c.versions.head_id() is not None}

    @public.get("/auth/status")
    def auth_status(request: Request):
        return {"configured": c.settings.has_login(),
                "authenticated": verify_token(config.secret_key, request.cookies.get(COOKIE))}

    def _set_cookie(resp: Response):
        resp.set_cookie(COOKIE, make_token(config.secret_key, config.session_hours), httponly=True,
                        samesite="strict", secure=config.cookie_secure, max_age=config.session_hours * 3600, path="/")

    @public.post("/auth/setup")
    def auth_setup(body: PasswordIn, response: Response):
        if c.settings.has_login():
            raise HTTPException(409, "Already configured")
        try:
            c.settings.set_login_password(body.password)
        except ValueError as e:
            raise HTTPException(422, str(e))
        _set_cookie(response)
        return {"ok": True}

    @public.post("/auth/login")
    def auth_login(body: PasswordIn, request: Request, response: Response):
        ip = request.client.host if request.client else "?"
        q = failures[ip]
        now = time.time()
        while q and now - q[0] > 300:
            q.popleft()
        if len(q) >= 8:
            raise HTTPException(429, "Too many attempts, try again in a few minutes")
        if not c.settings.check_login_password(body.password):
            q.append(now)
            raise HTTPException(401, "Wrong password")
        q.clear()
        _set_cookie(response)
        return {"ok": True}

    @public.get("/email/oauth/callback", include_in_schema=False)
    def email_callback(code: str | None = None, state: str | None = None, error: str | None = None):
        from urllib.parse import quote
        try:
            c.email.complete(code, state, error)
            return RedirectResponse("/settings?email=connected#email", status_code=303)
        except (EmailSyncError, GmailError) as e:
            return RedirectResponse(f"/settings?email_error={quote(str(e)[:300])}#email", status_code=303)

    @public.post("/auth/logout")
    def auth_logout(response: Response):
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    # ============================================================ protected
    api = APIRouter(prefix="/api", dependencies=[Depends(require_session)])

    # -------- portfolio (always valued at live prices; falls back to the last synced rate)
    @api.get("/portfolio/overview")
    def overview():
        snap = c.portfolio.snapshot()
        out = c.analytics.overview(snap)
        out["meta"]["version"] = c.versions.head()
        out["meta"]["price_sync"] = c.price_sync.status()
        return out

    # -------- tax planning
    @api.get("/tax/summary")
    def tax_summary():
        return c.tax.summary(c.portfolio.snapshot())

    @api.post("/tax/simulate-sale")
    def tax_simulate(body: SaleIn):
        try:
            return c.tax.simulate_sale(c.portfolio.snapshot(), body.name, units=body.units, amount=body.amount,
                                       sale_date=body.sale_date, price=body.price)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @api.post("/tax/raise-cash")
    def tax_raise_cash(body: RaiseCashIn):
        try:
            return c.tax.raise_cash(c.portfolio.snapshot(), body.amount, body.exclude)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @api.post("/tax/withdrawal-plan")
    def tax_withdrawals(body: WithdrawalIn):
        try:
            return c.tax.withdrawal_plan(c.portfolio.snapshot(), body.monthly_amount, body.months,
                                         body.annual_return_pct, body.start, body.exclude)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @api.put("/tax/realised")
    def tax_realised(body: RealisedIn):
        today = dt.date.today()
        c.settings.update({"realized_fy": fy_label(today), "realized_stcg_fy": body.st_equity,
                           "realized_ltcg_fy": body.lt_equity, "realized_st_other_fy": body.st_slab,
                           "realized_lt_other_fy": body.lt_other})
        return c.tax.summary(c.portfolio.snapshot())

    # -------- email import (Gmail)
    def base_url(request: Request) -> str:
        # honours X-Forwarded-Proto/Host behind a reverse proxy (uvicorn --proxy-headers)
        return str(request.base_url).rstrip("/")

    @api.get("/email/status")
    def email_status(request: Request):
        st = c.email.status()
        st["redirect_uri"] = c.email.redirect_uri_for(base_url(request), c.settings.get("gmail_redirect_uri"),
                                                      request.url.port)
        return st

    @api.put("/email/client")
    def email_client(body: EmailClientIn):
        return c.email.set_client(body.client_id, body.client_secret, body.client_json)

    @api.post("/email/connect")
    def email_connect(request: Request):
        return c.email.start(base_url(request))

    @api.post("/email/oauth/complete")
    def email_complete(body: EmailCompleteIn):
        return c.email.complete_from_url(body.url)

    @api.post("/email/disconnect")
    def email_disconnect():
        return c.email.disconnect()

    @api.post("/email/preview")
    def email_preview(body: EmailSyncIn):
        return c.email.preview(body.query, body.only_new)

    @api.post("/email/sync")
    def email_sync(body: EmailSyncIn):
        return c.email.sync(body.query, body.only_new, body.password or None, body.save_password)

    # -------- sells / bonus / splits (Transactions sheet)
    @api.get("/transactions")
    def txn_list():
        return c.transactions.list()

    @api.post("/transactions")
    def txn_add(body: TxnIn):
        try:
            return c.transactions.add(body.to_domain())
        except ValueError as e:
            raise HTTPException(422, str(e))

    @api.put("/transactions/{txn_id}")
    def txn_update(txn_id: str, body: TxnIn):
        try:
            return c.transactions.update(txn_id, body.to_domain(txn_id))
        except ValueError as e:
            raise HTTPException(422, str(e))

    @api.delete("/transactions/{txn_id}")
    def txn_delete(txn_id: str):
        try:
            return c.transactions.delete(txn_id)
        except ValueError as e:
            raise HTTPException(422, str(e))

    # -------- investment history (all asset classes)
    @api.get("/history/events")
    def history_events():
        return c.history.events()

    @api.get("/history/holdings")
    def history_holdings(on: dt.date | None = None):
        return c.history.holdings_on(on or dt.date.today())

    @api.get("/history/timeline")
    def history_timeline():
        return c.history.timeline()

    # -------- physical gold (Gold sheet + photos)
    @api.get("/gold")
    def gold_overview():
        return c.gold.overview()

    @api.post("/gold")
    def gold_add(body: GoldIn):
        try:
            return c.gold.add(body.to_domain())
        except ValueError as e:
            raise HTTPException(422, str(e))

    @api.put("/gold/{item_id}")
    def gold_update(item_id: str, body: GoldIn):
        try:
            return c.gold.update(item_id, body.to_domain(item_id))
        except ValueError as e:
            raise HTTPException(422, str(e))

    @api.delete("/gold/{item_id}")
    def gold_delete(item_id: str):
        return c.gold.delete(item_id)

    @api.post("/gold/{item_id}/photos")
    async def gold_photo_add(item_id: str, file: UploadFile = File(...)):
        data = await file.read(8 * 1024 * 1024 + 1)
        try:
            return c.gold.add_photo(item_id, data, file.filename)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @api.get("/gold/photos/{photo_id}")
    def gold_photo(photo_id: int):
        data, mime, name = c.gold.photo(photo_id)
        return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=86400",
                                                       "Content-Disposition": f'inline; filename="{name}"'})

    @api.delete("/gold/photos/{photo_id}")
    def gold_photo_delete(photo_id: int):
        c.gold.delete_photo(photo_id)
        return {"ok": True}

    # -------- price sync
    @api.get("/prices/sync")
    def price_sync_status():
        return c.price_sync.status()

    @api.post("/prices/sync")
    def price_sync_now():
        return c.price_sync.run_once("manual")

    @api.get("/portfolio/charges")
    def charges():
        return c.analytics.charges(c.versions.snapshot())

    @api.get("/portfolio/performance")
    def performance():
        return c.performance.compute()


    @api.get("/portfolio/ledger")
    def ledger():
        return c.repo.ledger()

    @api.get("/sheet/structure")
    def structure():
        snap = c.versions.snapshot()
        return {
            "sheet_name": snap.sheet_name,
            "valuation_date": snap.valuation_date,
            "sections": [dataclasses.asdict(s) for s in snap.sections],
            "summary": [dataclasses.asdict(s) for s in snap.summary],
            "sectors": [dataclasses.asdict(s) for s in snap.sectors],
            "sector_to_block": snap.sector_to_block,
            "overrides": c.settings.get("sector_block_overrides"),
            "totals": snap.totals,
            "issues": [dataclasses.asdict(i) for i in snap.issues],
            "blank_rows": snap.blank_rows,
        }

    @api.get("/sheet/download")
    def download_head():
        h = c.versions.head()
        if not h:
            raise NoSheetError("No tracking sheet uploaded yet")
        return Response(c.versions.head_bytes(), media_type=XLSX,
                        headers={"Content-Disposition": f'attachment; filename="Invt_v{h["id"]}.xlsx"'})

    # -------- versions
    @api.get("/versions")
    def versions():
        return c.versions.list()

    @api.get("/versions/{vid}/download")
    def version_download(vid: int):
        v = c.repo.version(vid)
        if not v:
            raise KeyError(vid)
        return Response(c.versions.bytes_of(vid), media_type=XLSX,
                        headers={"Content-Disposition": f'attachment; filename="Invt_v{vid}.xlsx"'})

    @api.get("/versions/{vid}/diff")
    def version_diff(vid: int, against: int | None = None):
        return c.versions.diff(vid, against)

    @api.post("/versions/{vid}/restore")
    def version_restore(vid: int):
        return c.versions.restore(vid)

    @api.post("/versions/upload")
    async def version_upload(file: UploadFile = File(...), message: str | None = Form(None)):
        data = await read_upload(file, "xlsx")
        return c.versions.upload(data, file.filename or "sheet.xlsx", message)

    # -------- imports
    @api.post("/imports")
    async def import_stage(files: list[UploadFile] = File(...), password: str | None = Form(None),
                           use_saved_password: bool = Form(True), save_password: bool = Form(False)):
        if len(files) > 100:
            raise HTTPException(413, "At most 100 files per upload")
        payload = [(f.filename or "note.pdf", await read_upload(f, "pdf")) for f in files]
        return c.imports.stage(payload, password or None, use_saved=use_saved_password, save_password=save_password)

    @api.get("/imports")
    def import_list():
        return c.imports.list()

    @api.get("/imports/{bid}")
    def import_get(bid: str):
        return c.imports.view(bid)

    @api.post("/imports/{bid}/retry")
    def import_retry(bid: str, body: RetryIn):
        return c.imports.retry(bid, body.password, save_password=body.save_password)

    @api.post("/imports/{bid}/refresh")
    def import_refresh(bid: str):
        return c.imports.refresh(bid)

    @api.post("/imports/{bid}/preview")
    def import_preview(bid: str, body: DecisionsIn):
        return c.imports.preview(bid, [d.model_dump(exclude_none=True) for d in body.decisions])

    @api.post("/imports/{bid}/commit")
    def import_commit(bid: str, body: DecisionsIn):
        return c.imports.commit(bid, [d.model_dump(exclude_none=True) for d in body.decisions], body.message)

    @api.delete("/imports/{bid}")
    def import_discard(bid: str):
        c.imports.discard(bid)
        return {"ok": True}

    # -------- settings
    @api.get("/settings")
    def settings_get():
        return c.settings.all()

    @api.put("/settings")
    def settings_put(patch: dict[str, Any]):
        patch.pop("has_pdf_password", None)
        try:
            return c.settings.update(patch)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @api.put("/settings/pdf-password")
    def pdf_password_set(body: PasswordIn):
        c.settings.set_pdf_password(body.password)
        return {"ok": True}

    @api.delete("/settings/pdf-password")
    def pdf_password_clear():
        c.settings.set_pdf_password(None)
        return {"ok": True}

    @api.put("/settings/login-password")
    def login_password(body: PasswordIn):
        try:
            c.settings.set_login_password(body.password)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"ok": True}

    @api.get("/securities")
    def securities():
        snap = c.versions.snapshot()
        syms = c.repo.symbols()
        aliases = c.repo.securities()
        return {
            "holdings": [{"name": n, "symbol": (syms.get(n) or {}).get("symbol"),
                          "source": (syms.get(n) or {}).get("source")} for n in sorted({l.name for l in snap.lots})],
            "aliases": aliases,
        }

    @api.put("/securities/{name}/symbol")
    def set_symbol(name: str, body: SymbolIn):
        c.repo.set_symbol(name, (body.symbol or "").strip().upper() or None, "manual")
        return {"ok": True}

    @api.get("/securities/search")
    def search(q: str):
        return c.prices.resolve(q.strip()[:60])

    @api.get("/audit")
    def audit():
        return c.repo.audit_log()

    app.include_router(public)
    app.include_router(api)

    # -------- SPA
    static = config.static_dir
    if static and Path(static).exists():
        app.mount("/assets", StaticFiles(directory=Path(static) / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            f = (Path(static) / path).resolve()
            if path and f.is_file() and Path(static).resolve() in f.parents:
                return FileResponse(f)
            return FileResponse(Path(static) / "index.html")

    return app
