# Portfolio Tracker

A self-hosted web app that reads broker contract notes (PDF, password-protected), adds the purchases to your Excel tracking sheet (`Invt.xlsx`) and turns that sheet into a portfolio dashboard and tax planner.

The workbook is the source of truth. The app writes to it in exactly two ways:
- **Imports** insert new rows (existing cells are never edited, formulas are never overwritten). Each import is saved as a new version.
- **Price sync** writes live market prices into the market-rate cell of existing purchase rows and the sheet's valuation date, at startup and then every 60 minutes, so the workbook matches the app when opened on its own. Formula cells are never touched. Price writes update the current version in place instead of creating new versions.

It also keeps two **extra sheets** of its own in the same workbook, added after your sheet. Writing them never changes your original sheet: after every write the app checks byte-for-byte that the original sheet's XML, the shared strings and the calc chain are unchanged, and aborts otherwise.
- **`Transactions`**: sales (from contract notes or entered by hand), bonus shares and splits. Holdings, P&L and tax use them automatically: units sold are taken oldest first, bonus shares are dated on allotment at zero cost.
- **`Gold`**: physical gold (coins, bars, jewellery) entered by hand, with the current rate and value refreshed by the price sync. Photos of items are kept in `data/photos/`.

## Run on a home server

The only requirement is Docker with Compose (Docker Engine + the compose plugin, Docker Desktop, or Colima). Everything else (Python, Node, the UI build, the tests) runs inside the image build.

```bash
cd portfolio-tracker                      # the project folder (copied or git-cloned)
cp /path/to/Invt.xlsx seed/Invt.xlsx      # optional: your existing sheet becomes version 1
docker compose up -d --build              # use `docker-compose up -d --build` with the standalone binary
```

Open `http://<server>:8000`, create the app password, and you're done. If you skipped the `seed/` step, the next screen asks you to upload `Invt.xlsx` in the browser. The first build takes a few minutes. `./data` is created automatically.

Useful commands: `docker compose logs -f` (logs), `docker compose down` (stop), `git pull && docker compose up -d --build` (upgrade, data is kept).

The image runs the test suite while it builds, so a broken build never produces an image. The container starts as root only long enough to `chown` `/data`, then runs as `PUID:PGID` (default 10001).

| Variable | Default | Purpose |
|---|---|---|
| `PT_DATA_DIR` | `/data` | Sheet versions, SQLite metadata, uploaded notes, gold photos, secret key |
| `PT_SEED_SHEET` | `/seed/Invt.xlsx` | Workbook imported as version 1 when no sheet exists (missing file = ignored) |
| `PT_LIVE_PRICES` | `true` | Live quotes (Yahoo Finance). `false` = offline: the app uses the market rates already in the sheet |
| `PT_PRICE_SYNC_MINUTES` | `60` | Default interval for writing prices into the sheet (also editable in Settings). `0` disables the background sync |
| `PT_COOKIE_SECURE` | `false` | Set to `true` behind an HTTPS reverse proxy |
| `PT_MAX_UPLOAD_MB` | `15` | Size limit per uploaded file |
| `PT_SESSION_HOURS` | `168` | Login session lifetime |
| `PUID` / `PGID` | `10001` | User the app runs as |
| `PT_SECRET_KEY` | generated | Overrides `data/secret.key`, which signs sessions and encrypts the saved PDF password |

### Getting the latest sheet from a terminal

`data/current/Invt.xlsx` is always an exact copy of the current version. It is replaced atomically on every import, restore, upload and price write, so it is never half-written. It's a read-only mirror: edits to it are ignored; upload edited sheets via **Sheet history → Upload edited sheet**.

```bash
# on the server, or from another machine
scp server:portfolio-tracker/data/current/Invt.xlsx .

# over HTTP (works from anywhere the app is reachable)
curl -s -c pt.jar -H 'Content-Type: application/json' -H 'X-Requested-With: portfolio-tracker' \
     -d '{"password":"YOUR_APP_PASSWORD"}' http://server:8000/api/auth/login
curl -s -b pt.jar -OJ http://server:8000/api/sheet/download        # saves Invt_v<N>.xlsx
curl -s -b pt.jar -OJ http://server:8000/api/versions/3/download    # any older version
```

Don't take files from `data/versions/`. They are named by content hash, and the current one is recorded only in the database.

### Storage

Versions are kept indefinitely. Each one is a full copy of the workbook (about 20 KB for this sheet) and identical content is stored once. Price writes don't add versions, and the replaced file is deleted. Ten years of three imports a week is roughly 1,600 versions, about 35 MB, plus the uploaded PDFs (about 50 KB each).

Back up the whole `data/` directory. If `secret.key` is lost, the saved PDF password can't be decrypted (you just enter it again), and everyone has to sign in again.

Security: one user, one password. Login attempts are rate-limited, cookies are HttpOnly and SameSite=Strict, a CSRF header is required, and security headers are set. Keep the app on your LAN or VPN. If you expose it to the internet, put it behind an HTTPS reverse proxy and set `PT_COOKIE_SECURE=true`.

## Using it

1. **Import notes.** Drop one or more contract-note PDFs and enter the password once. It can be stored encrypted for next time. Every file in one upload uses the same password.
2. **Review.** Each transaction is checked against the rows already in the sheet, including rows you typed in by hand, and labelled *New*, *Already in sheet* or *Possible duplicate*. For a new security, pick an existing sector or add a new one.
3. **Preview → Write.** You see exactly which rows will be inserted and the result of the safety checks. Confirming creates a new sheet version.
4. **Sales, bonus shares, gold.** Sales in contract notes are imported to the Transactions sheet. Record bonus shares, splits or sales from elsewhere on **Investment history → Sales, bonus & splits**, and physical gold on **Physical gold**. Gold prices, buy-back deductions and tax rules are editable in **Settings**.
5. **Sheet history.** Download any version, see what changed in it, restore it (a restore is saved as a new version), or upload a copy you edited in Excel.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements-dev.txt
cd backend && PYTHONPATH=. ../.venv/bin/python -m pytest -q          # tests
PT_DATA_DIR=../data ../.venv/bin/uvicorn app.main:app --reload       # API on :8000
cd ../frontend && npm ci && npm run dev                              # UI on :5173 (proxies /api)
```

The real sample files in `backend/tests/fixtures/` (your sheet and a contract note) contain personal data and are git-ignored. Without them, the tests that need them are skipped; copy them back to run all 83.

API docs: `/api/docs`. Architecture, design decisions and how to add another broker or sheet layout are covered in the project playbook (Google Doc).

## Layout

```
backend/app/
  domain/models.py           broker- and Excel-independent types
  dao/base.py                interfaces (PortfolioSheetDAO, ContractNoteParser, PriceProvider)
  dao/xlsx/                  OOXML row insertion, formula shifting, evaluator, Invt sheet DAO
  dao/contract_notes/        PDF decryption, broker parser registry, SBICAP parser
  dao/repo/                  SQLite metadata + content-addressed blob store
  dao/prices/yahoo.py        optional market data
  services/                  import workflow, matching/dedup, versions, analytics, settings
  api/                       FastAPI routes + auth
  container.py               composition root (the only place concrete DAOs are chosen)
frontend/src/                React + TypeScript UI
```
