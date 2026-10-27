"""Sync contract notes from Gmail: search, download PDF attachments to a temporary directory,
then hand them to the normal import workflow (parse -> review -> preview -> write).

Security:
* read-only Gmail scope; the refresh token and the OAuth client secret are stored
  encrypted (same key as the PDF password); nothing is sent anywhere except Google;
* the OAuth ``state`` is random, single-use, bound to the redirect URI and expires after 10
  minutes. It authenticates the browser redirect back from Google (the session cookie is
  SameSite=Strict, so it is not sent on that cross-site navigation);
* downloaded files live only in a per-sync temporary directory that is deleted afterwards
  (staged notes are kept like uploaded ones, in ``data/contract_notes``).
"""
from __future__ import annotations

import hashlib
import json
import secrets
import tempfile
import time
import urllib.parse
from pathlib import Path

from app.dao.mail.gmail import GmailAuthError, GmailClient, GmailError
from app.dao.repo.sqlite_repo import Repository
from app.services.import_service import ImportError_, ImportService
from app.services.settings_service import SettingsService

CALLBACK_PATH = "/api/email/oauth/callback"
STATE_TTL = 600
SECRET_CLIENT = "gmail_client_secret"
SECRET_REFRESH = "gmail_refresh_token"


class EmailSyncError(Exception):
    pass


class EmailSyncService:
    def __init__(self, repo: Repository, settings: SettingsService, imports: ImportService, tmp_root: Path,
                 max_file_bytes: int, gmail: GmailClient | None = None):
        self.repo = repo
        self.settings = settings
        self.imports = imports
        self.tmp_root = tmp_root
        self.max_file_bytes = max_file_bytes
        self.gmail = gmail or GmailClient()
        self._access: tuple[str, float] | None = None

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        c = self.settings.all()
        return {
            "client_id": c["gmail_client_id"], "has_client_secret": self.settings.get_secret(SECRET_CLIENT) is not None,
            "connected": self.settings.get_secret(SECRET_REFRESH) is not None,
            "account": self.repo.get("gmail_account"), "connected_at": self.repo.get("gmail_connected_at"),
            "query": c["gmail_query"], "only_new": c["gmail_only_new"], "max_messages": c["gmail_max_messages"],
            "last_sync": self.repo.get("gmail_last_sync"),
        }

    def set_client(self, client_id: str | None = None, client_secret: str | None = None,
                   client_json: str | None = None) -> dict:
        if client_json:
            try:
                d = json.loads(client_json)
                d = d.get("web") or d.get("installed") or d
                client_id, client_secret = d["client_id"], d["client_secret"]
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                raise EmailSyncError("That is not a Google OAuth client JSON file (expected client_id and client_secret)") from e
        if not client_id or not client_id.strip().endswith(".apps.googleusercontent.com"):
            raise EmailSyncError("The client ID should end with .apps.googleusercontent.com")
        if client_id.strip() != self.settings.get("gmail_client_id"):
            self.disconnect()  # tokens belong to the old client
        self.settings.update({"gmail_client_id": client_id.strip()})
        if client_secret:
            self.settings.set_secret(SECRET_CLIENT, client_secret.strip())
        return self.status()

    # ------------------------------------------------------------------- OAuth
    @staticmethod
    def redirect_uri_for(base_url: str, configured: str, port: int | None) -> str:
        """Google accepts https:// or http://localhost redirect URIs only (no LAN IPs)."""
        if configured:
            return configured
        u = urllib.parse.urlsplit(base_url)
        host = (u.hostname or "").lower()
        if u.scheme == "https" or host in ("localhost", "127.0.0.1", "::1"):
            return f"{u.scheme}://{u.netloc}{CALLBACK_PATH}"
        return f"http://localhost:{port or u.port or 8000}{CALLBACK_PATH}"

    def start(self, base_url: str) -> dict:
        c = self.settings.all()
        secret = self.settings.get_secret(SECRET_CLIENT)
        if not c["gmail_client_id"] or not secret:
            raise EmailSyncError("Add your Google OAuth client ID and secret first")
        redirect = self.redirect_uri_for(base_url, c["gmail_redirect_uri"], urllib.parse.urlsplit(base_url).port)
        state = secrets.token_urlsafe(32)
        pending = {k: v for k, v in (self.repo.get("gmail_oauth_states") or {}).items() if v["exp"] > time.time()}
        pending[state] = {"exp": time.time() + STATE_TTL, "redirect_uri": redirect}
        self.repo.put("gmail_oauth_states", pending)
        return {"url": self.gmail.auth_url(c["gmail_client_id"], redirect, state), "redirect_uri": redirect,
                "direct": redirect.startswith(base_url.rstrip("/"))}

    def complete(self, code: str | None, state: str | None, error: str | None = None) -> dict:
        if error:
            raise EmailSyncError("Google sign-in was cancelled" if error == "access_denied" else f"Google returned: {error}")
        pending = self.repo.get("gmail_oauth_states") or {}
        entry = pending.pop(state or "", None)
        self.repo.put("gmail_oauth_states", pending)  # single use
        if not entry or entry["exp"] < time.time():
            raise EmailSyncError("This sign-in link has expired or was already used; click Connect again")
        if not code:
            raise EmailSyncError("Google did not return an authorisation code")
        tok = self.gmail.exchange(code, self.settings.get("gmail_client_id"), self.settings.get_secret(SECRET_CLIENT),
                                  entry["redirect_uri"])
        if not tok.refresh_token:
            raise EmailSyncError("Google did not return a refresh token; remove the app's access at "
                                 "myaccount.google.com/permissions and connect again")
        self.settings.set_secret(SECRET_REFRESH, tok.refresh_token)
        self._access = (tok.access_token, tok.expires_at)
        self.repo.put("gmail_account", self.gmail.profile(tok.access_token))
        self.repo.put("gmail_connected_at", time.time())
        self.repo.audit("email.connect", {"account": self.repo.get("gmail_account")})
        return self.status()

    def complete_from_url(self, pasted: str) -> dict:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(pasted.strip()).query)
        if not q.get("code") and not q.get("error"):
            raise EmailSyncError("Paste the full address from the browser's address bar after signing in "
                                 "(it contains ?code=…&state=…)")
        return self.complete(q.get("code", [None])[0], q.get("state", [None])[0], q.get("error", [None])[0])

    def disconnect(self) -> dict:
        tok = self.settings.get_secret(SECRET_REFRESH)
        if tok:
            self.gmail.revoke(tok)
        self.settings.set_secret(SECRET_REFRESH, None)
        self.repo.put("gmail_account", None)
        self._access = None
        self.repo.audit("email.disconnect", {})
        return self.status()

    def _access_token(self) -> str:
        if self._access and self._access[1] > time.time():
            return self._access[0]
        refresh = self.settings.get_secret(SECRET_REFRESH)
        if not refresh:
            raise EmailSyncError("Gmail is not connected; connect it in Settings → Email")
        try:
            tok = self.gmail.refresh(refresh, self.settings.get("gmail_client_id"), self.settings.get_secret(SECRET_CLIENT))
        except GmailAuthError as e:
            self.settings.set_secret(SECRET_REFRESH, None)
            raise EmailSyncError(f"{e}. If your Google Cloud app is in 'Testing' mode, Google expires the "
                                 "access after 7 days - publish it to 'In production' and connect again.") from e
        self._access = (tok.access_token, tok.expires_at)
        return tok.access_token

    # -------------------------------------------------------------------- sync
    def _already_synced(self) -> set[str]:
        out = set()
        for r in self.repo.mail_synced():
            b = self.repo.batch(r["batch_id"]) if r["batch_id"] else None
            if b is None or b["status"] != "discarded":  # discarded imports may be fetched again
                out.add(r["message_id"])
        return out

    def preview(self, query: str | None = None, only_new: bool | None = None) -> dict:
        """List matching emails (no downloads) so the user can see what a sync would fetch."""
        c = self.settings.all()
        q = (query if query is not None else c["gmail_query"]).strip()
        only_new = c["gmail_only_new"] if only_new is None else only_new
        access = self._access_token()
        ids = self.gmail.search(access, self._full_query(q), int(c["gmail_max_messages"]))
        done = self._already_synced()
        out = []
        for i in ids:
            m = self.gmail.message(access, i)
            pdfs = [a.filename for a in m.attachments if self._is_pdf(a)]
            out.append({"id": m.id, "subject": m.subject, "from": m.sender, "date": m.date, "pdfs": pdfs,
                        "already_synced": m.id in done, "will_fetch": bool(pdfs) and not (only_new and m.id in done)})
        return {"query": q, "full_query": self._full_query(q), "emails": out}

    @staticmethod
    def _full_query(q: str) -> str:
        return f"({q}) has:attachment" if q else "has:attachment filename:pdf"

    @staticmethod
    def _is_pdf(a) -> bool:
        return a.mime == "application/pdf" or a.filename.lower().endswith(".pdf")

    def sync(self, query: str | None, only_new: bool | None, password: str | None, save_password: bool) -> dict:
        c = self.settings.all()
        q = (query if query is not None else c["gmail_query"]).strip()
        only_new = c["gmail_only_new"] if only_new is None else only_new
        if query is not None and q != c["gmail_query"]:
            self.settings.update({"gmail_query": q})  # remember the last search
        access = self._access_token()
        ids = self.gmail.search(access, self._full_query(q), int(c["gmail_max_messages"]))
        done = self._already_synced() if only_new else set()
        summary = {"query": q, "emails_found": len(ids), "emails_skipped_already_synced": 0, "emails_without_pdf": 0,
                   "pdfs": 0, "duplicates_skipped": 0, "too_large": [], "messages": []}
        self.tmp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.tmp_root, prefix="mail-") as tmp:
            files: list[tuple[str, bytes]] = []
            seen: set[str] = set()
            used_msgs: list[str] = []
            for mid in ids:
                if mid in done:
                    summary["emails_skipped_already_synced"] += 1
                    continue
                m = self.gmail.message(access, mid)
                pdfs = [a for a in m.attachments if self._is_pdf(a)]
                if not pdfs:
                    summary["emails_without_pdf"] += 1
                    continue
                got = handled = 0  # handled: downloaded, or identical to a file already in this sync
                for a in pdfs:
                    if a.size and a.size > self.max_file_bytes:
                        summary["too_large"].append(a.filename)
                        continue
                    data = self.gmail.attachment(access, mid, a)
                    if len(data) > self.max_file_bytes:
                        summary["too_large"].append(a.filename)
                        continue
                    sha = hashlib.sha256(data).hexdigest()
                    if sha in seen:
                        summary["duplicates_skipped"] += 1
                        handled += 1
                        continue
                    seen.add(sha)
                    name = Path(a.filename).name or f"{mid}.pdf"
                    path = Path(tmp) / f"{len(files):03d}-{name}"
                    path.write_bytes(data)
                    files.append((name, path.read_bytes()))
                    got += 1
                    handled += 1
                if handled:
                    used_msgs.append(mid)
                if got:
                    summary["messages"].append({"id": mid, "subject": m.subject, "date": m.date, "pdfs": got})
            summary["pdfs"] = len(files)
            self.repo.put("gmail_last_sync", {"at": time.time(), **{k: v for k, v in summary.items() if k != "messages"}})
            if not files:
                return {"batch": None, "summary": summary}
            try:
                batch = self.imports.stage(files, password, use_saved=True, save_password=save_password)
            except ImportError_ as e:
                raise EmailSyncError(str(e)) from e
        # temporary directory is gone here; staged copies live in data/contract_notes
        self.repo.add_mail_synced(used_msgs, batch["id"])
        self.repo.audit("email.sync", {"query": q, "emails": len(used_msgs), "pdfs": len(files), "batch": batch["id"]})
        return {"batch": batch, "summary": summary}


__all__ = ["EmailSyncService", "EmailSyncError", "GmailError", "CALLBACK_PATH"]
