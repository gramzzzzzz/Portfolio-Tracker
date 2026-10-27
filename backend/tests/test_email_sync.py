"""Sync from email: OAuth flow + Gmail search/attachments against a mocked Google."""
import base64
import json
import urllib.parse

import httpx
import pytest

from app.config import Config
from app.container import build
from app.dao.mail.gmail import GmailClient

CID = "123-abc.apps.googleusercontent.com"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class FakeGoogle:
    """Implements the 6 endpoints the app calls. Mailbox: m1 (note PDF), m2 (same PDF again,
    plus a .txt), m3 (no attachment), m4 (a second, different PDF given inline)."""

    def __init__(self, note: bytes, other: bytes):
        self.note, self.other = note, other
        self.queries: list[str] = []
        self.revoked: list[str] = []
        self.refresh_ok = True
        self.issued_refresh = "refresh-1"

    def __call__(self, req: httpx.Request) -> httpx.Response:
        url = req.url
        if url.host == "oauth2.googleapis.com" and url.path == "/token":
            form = urllib.parse.parse_qs(req.content.decode())
            if form["client_id"] != [CID] or form["client_secret"] != ["s3cret"]:
                return httpx.Response(401, json={"error": "invalid_client"})
            if form["grant_type"] == ["authorization_code"]:
                if form["code"] != ["good-code"]:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json={"access_token": "acc-1", "expires_in": 3600, "refresh_token": self.issued_refresh})
            if not self.refresh_ok:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
            return httpx.Response(200, json={"access_token": "acc-2", "expires_in": 3600})
        if url.path == "/revoke":
            self.revoked.append(url.params["token"])
            return httpx.Response(200)
        assert req.headers["authorization"].startswith("Bearer acc-")
        if url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "me@example.com"})
        if url.path.endswith("/messages"):
            self.queries.append(url.params["q"])
            return httpx.Response(200, json={"messages": [{"id": i} for i in ("m1", "m2", "m3", "m4")]})
        if "/attachments/" in url.path:
            return httpx.Response(200, json={"data": b64(self.note), "size": len(self.note)})
        mid = url.path.rsplit("/", 1)[-1]
        parts = {
            "m1": [{"filename": "CNB_11_note.pdf", "mimeType": "application/pdf", "body": {"attachmentId": "a1", "size": len(self.note)}}],
            "m2": [{"filename": "CNB_11_again.pdf", "mimeType": "application/pdf", "body": {"attachmentId": "a2", "size": len(self.note)}},
                   {"filename": "readme.txt", "mimeType": "text/plain", "body": {"attachmentId": "a3", "size": 5}}],
            "m3": [],
            "m4": [{"filename": "other.PDF", "mimeType": "application/octet-stream", "body": {"data": b64(self.other), "size": len(self.other)}}],
        }[mid]
        return httpx.Response(200, json={"id": mid, "payload": {"headers": [{"name": "Subject", "value": f"Contract note {mid}"}],
                                                                 "parts": [{"mimeType": "text/plain", "body": {"size": 1}}] + parts}})


@pytest.fixture
def env(tmp_path, sheet_bytes, note_bytes, encrypted_note):
    google = FakeGoogle(note_bytes, encrypted_note)
    c = build(Config(tmp_path, None, b"k", 1, 5, False, None, False, 0), price_provider=None,
              gmail=GmailClient(httpx.Client(transport=httpx.MockTransport(google))))
    c.versions.upload(sheet_bytes, "Invt.xlsx")
    return c, google


def connect(c, base="http://localhost:8000"):
    c.email.set_client(CID, "s3cret")
    start = c.email.start(base)
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(start["url"]).query)["state"][0]
    return start, state


def test_redirect_uri_rules():
    r = EmailSync = __import__("app.services.email_sync_service", fromlist=["x"]).EmailSyncService.redirect_uri_for
    assert r("http://localhost:8000", "", 8000) == "http://localhost:8000/api/email/oauth/callback"
    assert r("https://pt.example.com", "", None) == "https://pt.example.com/api/email/oauth/callback"
    # Google refuses LAN IPs -> fall back to localhost (paste flow) on the same port
    assert r("http://192.168.1.20:8000", "", 8000) == "http://localhost:8000/api/email/oauth/callback"
    assert r("http://192.168.1.20:8000", "https://custom/cb", 8000) == "https://custom/cb"
    assert EmailSync


def test_connect_flow_state_is_single_use_and_tokens_encrypted(env):
    c, google = env
    start, state = connect(c)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(start["url"]).query)
    assert q["scope"] == ["https://www.googleapis.com/auth/gmail.readonly"] and q["access_type"] == ["offline"]
    st = c.email.complete("good-code", state)
    assert st["connected"] and st["account"] == "me@example.com"
    raw = c.repo.get_secret("gmail_refresh_token")
    assert b"refresh-1" not in raw  # stored encrypted
    with pytest.raises(Exception, match="expired or was already used"):
        c.email.complete("good-code", state)  # replay


def test_paste_flow_and_errors(env):
    c, _ = env
    _, state = connect(c, "http://192.168.1.20:8000")
    with pytest.raises(Exception, match="Paste the full address"):
        c.email.complete_from_url("http://localhost:8000/api/email/oauth/callback")
    st = c.email.complete_from_url(f"http://localhost:8000/api/email/oauth/callback?state={state}&code=good-code&scope=x")
    assert st["connected"]
    _, state = connect(c)
    with pytest.raises(Exception, match="cancelled"):
        c.email.complete_from_url(f"http://localhost:8000/api/email/oauth/callback?error=access_denied&state={state}")


def test_client_json_and_validation(env):
    c, _ = env
    st = c.email.set_client(client_json=json.dumps({"web": {"client_id": CID, "client_secret": "s3cret"}}))
    assert st["client_id"] == CID and st["has_client_secret"]
    with pytest.raises(Exception):
        c.email.set_client("not-a-client-id", "x")


def test_sync_downloads_pdfs_and_stages_an_import(env):
    c, google = env
    _, state = connect(c)
    c.email.complete("good-code", state)
    c.settings.set_pdf_password("EHJPG1234")  # m4 is the encrypted copy of the note
    out = c.email.sync(None, None, None, False)
    s = out["summary"]
    assert google.queries == ["(cnb 11 contract) has:attachment"]
    assert s["emails_found"] == 4 and s["emails_without_pdf"] == 1 and s["pdfs"] == 2 and s["duplicates_skipped"] == 1
    b = out["batch"]
    assert [f["status"] for f in b["files"]] == ["parsed", "duplicate_file"]  # same note, two different PDFs
    assert b["summary"]["new"] == 2
    assert list((c.config.data_dir / "tmp").iterdir()) == []  # temporary directory cleaned up
    # second sync: those emails were already synced -> nothing to do
    again = c.email.sync(None, None, None, False)
    assert again["batch"] is None and again["summary"]["emails_skipped_already_synced"] == 3
    # discarding the import makes those emails eligible again
    c.imports.discard(b["id"])
    assert c.email.sync(None, None, None, False)["summary"]["pdfs"] == 2


def test_custom_query_is_remembered(env):
    c, google = env
    _, state = connect(c)
    c.email.complete("good-code", state)
    c.email.sync("from:sbicap newer_than:30d", False, None, False)
    assert google.queries[-1] == "(from:sbicap newer_than:30d) has:attachment"
    assert c.email.status()["query"] == "from:sbicap newer_than:30d"


def test_expired_refresh_token_explains_testing_mode(env):
    c, google = env
    _, state = connect(c)
    c.email.complete("good-code", state)
    c.email._access = None
    google.refresh_ok = False
    with pytest.raises(Exception, match="In production"):
        c.email.sync(None, None, None, False)
    assert not c.email.status()["connected"]


def test_disconnect_revokes(env):
    c, google = env
    _, state = connect(c)
    c.email.complete("good-code", state)
    c.email.disconnect()
    assert google.revoked == ["refresh-1"] and not c.email.status()["connected"]


def test_api_routes(app_client, sheet_bytes, tmp_path):
    from tests.test_api import login, upload_sheet
    c = app_client
    login(c)
    upload_sheet(c, sheet_bytes)
    st = c.get("/api/email/status").json()
    assert st["query"] == "cnb 11 contract" and st["connected"] is False
    assert st["redirect_uri"].endswith("/api/email/oauth/callback")
    assert c.post("/api/email/connect").status_code == 422  # no client yet
    assert c.put("/api/email/client", json={"client_id": CID, "client_secret": "s3cret"}).status_code == 200
    r = c.post("/api/email/connect").json()
    assert r["url"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    # the browser callback is public but useless without a valid state
    resp = c.get("/api/email/oauth/callback?code=x&state=forged", follow_redirects=False)
    assert resp.status_code == 303 and "email_error=" in resp.headers["location"]
    assert c.post("/api/email/sync", json={}).status_code == 422  # not connected
