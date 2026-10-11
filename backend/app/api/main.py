"""HTTP API (FastAPI). Thin layer: validation, auth, mapping service errors to HTTP."""
from __future__ import annotations

import dataclasses
import logging
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.api.auth import COOKIE, make_token, require_session, verify_token
from app.config import Config, load_config
from app.container import Container, build
from app.dao.base import LayoutError
from app.services.import_service import ImportError_
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


def create_app(config: Config | None = None, container: Container | None = None) -> FastAPI:
    config = config or load_config()
    c = container or build(config)
    app = FastAPI(title="Portfolio Tracker", version="1.0.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
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

    @public.post("/auth/logout")
    def auth_logout(response: Response):
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    # ============================================================ protected
    api = APIRouter(prefix="/api", dependencies=[Depends(require_session)])

    def live_flag(live: bool | None) -> bool:
        return c.settings.get("live_prices") if live is None else (live and c.settings.get("live_prices"))

    # -------- portfolio
    @api.get("/portfolio/overview")
    def overview(live: bool | None = None):
        snap = c.versions.snapshot()
        out = c.analytics.overview(snap, live_flag(live))
        out["meta"]["version"] = c.versions.head()
        return out

    @api.get("/portfolio/tax")
    def tax(live: bool | None = None):
        return c.analytics.tax(c.versions.snapshot(), live_flag(live))

    @api.get("/portfolio/charges")
    def charges():
        return c.analytics.charges(c.versions.snapshot())

    @api.get("/portfolio/benchmark")
    def benchmark():
        return c.analytics.benchmark(c.versions.snapshot())

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
