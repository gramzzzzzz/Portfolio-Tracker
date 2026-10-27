"""Gmail DAO: Google OAuth 2.0 (web-server flow) + the four Gmail API calls the app needs.

Read-only scope (``gmail.readonly``). Uses plain HTTPS calls through an injectable
``httpx.Client`` (no Google SDK), which keeps the dependency list short and makes the whole
flow testable with a mock transport.
"""
from __future__ import annotations

import base64
import time
import urllib.parse
from dataclasses import dataclass

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


class GmailError(Exception):
    pass


class GmailAuthError(GmailError):
    """The stored authorisation is no longer valid (revoked, expired, wrong client)."""


@dataclass
class Tokens:
    access_token: str
    expires_at: float
    refresh_token: str | None = None


@dataclass
class Attachment:
    filename: str
    mime: str
    size: int
    attachment_id: str | None
    inline_data: str | None  # small attachments come inline (base64url)


@dataclass
class MailMessage:
    id: str
    subject: str
    sender: str
    date: str
    attachments: list[Attachment]


def _b64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class GmailClient:
    def __init__(self, http: httpx.Client | None = None):
        self.http = http or httpx.Client(timeout=30.0, follow_redirects=False)

    # ------------------------------------------------------------------ OAuth
    @staticmethod
    def auth_url(client_id: str, redirect_uri: str, state: str) -> str:
        q = {
            "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE,
            "access_type": "offline",  # we need a refresh token
            "prompt": "consent",  # always return a refresh token, even on re-connect
            "include_granted_scopes": "true", "state": state,
        }
        return AUTH_URL + "?" + urllib.parse.urlencode(q)

    def _token(self, data: dict) -> Tokens:
        try:
            r = self.http.post(TOKEN_URL, data=data)
        except httpx.HTTPError as e:
            raise GmailError(f"Could not reach Google: {e}") from e
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200:
            err = body.get("error", r.status_code)
            desc = body.get("error_description", "")
            if err in ("invalid_grant", "invalid_client", "unauthorized_client"):
                raise GmailAuthError(f"Google rejected the authorisation ({err}{': ' + desc if desc else ''})")
            raise GmailError(f"Google token error: {err} {desc}".strip())
        return Tokens(body["access_token"], time.time() + int(body.get("expires_in", 3600)) - 60,
                      body.get("refresh_token"))

    def exchange(self, code: str, client_id: str, client_secret: str, redirect_uri: str) -> Tokens:
        return self._token({"code": code, "client_id": client_id, "client_secret": client_secret,
                            "redirect_uri": redirect_uri, "grant_type": "authorization_code"})

    def refresh(self, refresh_token: str, client_id: str, client_secret: str) -> Tokens:
        return self._token({"refresh_token": refresh_token, "client_id": client_id, "client_secret": client_secret,
                            "grant_type": "refresh_token"})

    def revoke(self, token: str) -> None:
        try:
            self.http.post(REVOKE_URL, params={"token": token})
        except httpx.HTTPError:
            pass  # best effort; the token is deleted locally anyway

    # ------------------------------------------------------------------- Gmail
    def _get(self, access: str, path: str, params: dict | None = None) -> dict:
        try:
            r = self.http.get(API + path, params=params, headers={"Authorization": f"Bearer {access}"})
        except httpx.HTTPError as e:
            raise GmailError(f"Could not reach Gmail: {e}") from e
        if r.status_code == 401:
            raise GmailAuthError("Gmail access was rejected; please reconnect")
        if r.status_code == 403:
            raise GmailError("Gmail refused the request (is the Gmail API enabled for your Google Cloud project?)")
        if r.status_code != 200:
            raise GmailError(f"Gmail error {r.status_code}: {r.text[:200]}")
        return r.json()

    def profile(self, access: str) -> str:
        return self._get(access, "/profile").get("emailAddress", "")

    def search(self, access: str, query: str, limit: int) -> list[str]:
        ids: list[str] = []
        token = None
        while len(ids) < limit:
            params = {"q": query, "maxResults": min(100, limit - len(ids))}
            if token:
                params["pageToken"] = token
            body = self._get(access, "/messages", params)
            ids += [m["id"] for m in body.get("messages", [])]
            token = body.get("nextPageToken")
            if not token:
                break
        return ids[:limit]

    def message(self, access: str, msg_id: str) -> MailMessage:
        m = self._get(access, f"/messages/{urllib.parse.quote(msg_id)}", {"format": "full"})
        payload = m.get("payload") or {}
        headers = {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers", [])}
        atts: list[Attachment] = []

        def walk(part):
            fn = part.get("filename") or ""
            body = part.get("body") or {}
            if fn and (body.get("attachmentId") or body.get("data")):
                atts.append(Attachment(fn, part.get("mimeType", ""), int(body.get("size") or 0),
                                       body.get("attachmentId"), body.get("data")))
            for p in part.get("parts") or []:
                walk(p)

        walk(payload)
        return MailMessage(m["id"], headers.get("subject", ""), headers.get("from", ""), headers.get("date", ""), atts)

    def attachment(self, access: str, msg_id: str, att: Attachment) -> bytes:
        if att.inline_data:
            return _b64url(att.inline_data)
        body = self._get(access, f"/messages/{urllib.parse.quote(msg_id)}/attachments/{urllib.parse.quote(att.attachment_id)}")
        return _b64url(body.get("data", ""))
