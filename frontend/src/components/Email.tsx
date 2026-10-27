import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api, type Batch } from "../api";
import { Alert, Badge, Card, Icon, useAsync, useToast } from "./ui";
import { dateTime } from "../format";

export interface EmailStatus {
  client_id: string;
  has_client_secret: boolean;
  connected: boolean;
  account: string | null;
  connected_at: number | null;
  query: string;
  only_new: boolean;
  max_messages: number;
  redirect_uri: string;
  last_sync: { at: number; emails_found: number; pdfs: number } | null;
}

interface SyncResult {
  batch: Batch | null;
  summary: { query: string; emails_found: number; emails_skipped_already_synced: number; emails_without_pdf: number; pdfs: number; duplicates_skipped: number; too_large: string[] };
}

/** Settings → Email: Google OAuth client + connect / disconnect. */
export function EmailSettings() {
  const toast = useToast();
  const { data: st, reload } = useAsync(() => api.get<EmailStatus>("/api/email/status"), []);
  const [cid, setCid] = useState("");
  const [secret, setSecret] = useState("");
  const [json, setJson] = useState("");
  const [err, setErr] = useState("");
  const [paste, setPaste] = useState<string | null>(null);
  const [pasted, setPasted] = useState("");
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState<string | null>(null);
  const [maxN, setMaxN] = useState<string | null>(null);

  // returning from Google's sign-in page (?email=connected / ?email_error=...)
  useEffect(() => {
    const p = new URLSearchParams(location.search);
    if (p.get("email") === "connected") toast("Gmail connected");
    if (p.get("email_error")) setErr(p.get("email_error")!);
    if (p.get("email") || p.get("email_error")) history.replaceState(null, "", location.pathname + location.hash);
  }, [toast]);
  useEffect(() => {
    if (st && !cid) setCid(st.client_id);
  }, [st, cid]);
  if (!st) return null;

  const run = async (fn: () => Promise<unknown>, ok?: string) => {
    setBusy(true);
    setErr("");
    try {
      await fn();
      if (ok) toast(ok);
      reload();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const saveClient = (e: FormEvent) => {
    e.preventDefault();
    run(() => api.send("PUT", "/api/email/client", json.trim() ? { client_json: json } : { client_id: cid, client_secret: secret || undefined }), "Google client saved").then(() => {
      setSecret("");
      setJson("");
    });
  };
  const connect = () =>
    run(async () => {
      const r = await api.send<{ url: string; redirect_uri: string; direct: boolean }>("POST", "/api/email/connect");
      if (r.direct) {
        location.href = r.url; // Google will send the browser straight back here
      } else {
        window.open(r.url, "_blank", "noopener");
        setPaste(r.redirect_uri);
      }
    });

  return (
    <section id="email">
      <Card title={<span className="row"><Icon name="upload" /> Email (Gmail)</span>} subtitle="Fetch contract notes straight from your inbox. Read-only access; nothing is sent anywhere except Google. Tokens are stored encrypted on this server.">
        <div className="stack">
          {err && <Alert tone="neg">{err}</Alert>}
          <div className="row">
            {st.connected ? <Badge tone="pos">Connected{st.account ? ` as ${st.account}` : ""}</Badge> : <Badge>Not connected</Badge>}
            {st.connected && st.connected_at && <span className="muted small">since {dateTime(st.connected_at)}</span>}
            {st.last_sync && <span className="muted small">· last sync {dateTime(st.last_sync.at)}: {st.last_sync.pdfs} PDF(s) from {st.last_sync.emails_found} email(s)</span>}
          </div>

          {!st.connected && (
            <details open={!st.client_id}>
              <summary>
                <strong>One-time setup in Google Cloud</strong> <span className="muted small">(about 5 minutes)</span>
              </summary>
              <ol className="small" style={{ margin: "8px 0 0", paddingLeft: 20, lineHeight: 1.7 }}>
                <li>
                  Open <a href="https://console.cloud.google.com/projectcreate" target="_blank" rel="noopener">console.cloud.google.com</a> and create a project (any name).
                </li>
                <li>
                  Enable the <a href="https://console.cloud.google.com/apis/library/gmail.googleapis.com" target="_blank" rel="noopener">Gmail API</a> for it.
                </li>
                <li>
                  <a href="https://console.cloud.google.com/auth/branding" target="_blank" rel="noopener">Google Auth Platform</a>: choose <em>External</em>, fill in the app name and your email. Under
                  <em> Audience</em> add yourself as a test user, then click <strong>Publish app</strong> — in “Testing” mode Google signs you out every 7 days.
                </li>
                <li>
                  <a href="https://console.cloud.google.com/auth/clients" target="_blank" rel="noopener">Clients</a> → Create client → <em>Web application</em>. Under <em>Authorized redirect URIs</em> add exactly:
                  <div className="row" style={{ marginTop: 4 }}>
                    <code style={{ padding: "2px 6px", background: "var(--surface-2)", borderRadius: 6 }}>{st.redirect_uri}</code>
                    <button className="btn ghost sm" type="button" onClick={() => navigator.clipboard?.writeText(st.redirect_uri).then(() => toast("Copied"))}>
                      Copy
                    </button>
                  </div>
                </li>
                <li>Copy the client ID and secret (or download the JSON) into the form below, then click Connect. Google will warn the app is unverified — it is your own app: choose <em>Continue</em>.</li>
              </ol>
              {st.redirect_uri.startsWith("http://localhost") && !location.hostname.match(/^(localhost|127\.0\.0\.1)$/) && (
                <p className="small muted" style={{ marginBottom: 0 }}>
                  Google only returns to <code>https://</code> or <code>localhost</code> addresses, not your server's LAN address. After you approve, the browser will show “can't reach localhost” — that's expected: copy that page's
                  address and paste it here. (Or open this app via HTTPS / <code>http://localhost</code> and it completes automatically.)
                </p>
              )}
            </details>
          )}

          <form className="stack" onSubmit={saveClient}>
            <div className="form-grid">
              <label className="field">
                <span>OAuth client ID</span>
                <input className="input" value={cid} onChange={(e) => setCid(e.target.value)} placeholder="…apps.googleusercontent.com" />
              </label>
              <label className="field">
                <span>Client secret {st.has_client_secret && <Badge tone="pos">saved</Badge>}</span>
                <input className="input" type="password" autoComplete="off" value={secret} onChange={(e) => setSecret(e.target.value)} placeholder={st.has_client_secret ? "unchanged" : ""} />
              </label>
            </div>
            <details>
              <summary className="small">…or paste the downloaded client JSON</summary>
              <textarea className="input" style={{ height: 80, padding: 8, marginTop: 6 }} value={json} onChange={(e) => setJson(e.target.value)} placeholder='{"web":{"client_id":"…","client_secret":"…"}}' />
            </details>
            <div className="row">
              <button className="btn" disabled={busy || (!cid && !json)}>
                Save client
              </button>
              {!st.connected ? (
                <button type="button" className="btn primary" disabled={busy || !st.client_id || !st.has_client_secret} onClick={connect}>
                  Connect Gmail
                </button>
              ) : (
                <button type="button" className="btn danger" disabled={busy} onClick={() => run(() => api.send("POST", "/api/email/disconnect"), "Gmail disconnected")}>
                  Disconnect
                </button>
              )}
            </div>
          </form>

          {paste && (
            <Alert tone="info" title="Finish signing in">
              <div className="stack" style={{ marginTop: 6 }}>
                <span className="small">A Google tab opened. After you approve, it lands on a “can't be reached” page at {paste.replace(/\/api.*/, "")} — copy that page's full address and paste it here:</span>
                <div className="row">
                  <input className="input" style={{ flex: 1, minWidth: 260 }} value={pasted} onChange={(e) => setPasted(e.target.value)} placeholder="http://localhost:8000/api/email/oauth/callback?state=…&code=…" aria-label="Address after signing in" />
                  <button className="btn primary" disabled={busy || !pasted} onClick={() => run(() => api.send("POST", "/api/email/oauth/complete", { url: pasted }), "Gmail connected").then(() => { setPaste(null); setPasted(""); })}>
                    Finish
                  </button>
                </div>
              </div>
            </Alert>
          )}

          <div className="form-grid">
            <label className="field">
              <span>Search for contract-note emails</span>
              <input className="input" value={query ?? st.query} onChange={(e) => setQuery(e.target.value)} />
              <span className="hint">Gmail search syntax, e.g. <code>cnb 11 contract newer_than:90d</code>. Only emails with attachments are fetched.</span>
            </label>
            <label className="field">
              <span>Emails per sync (max)</span>
              <input className="input" type="number" min={1} max={500} value={maxN ?? String(st.max_messages)} onChange={(e) => setMaxN(e.target.value)} />
            </label>
          </div>
          <div className="row">
            <button
              className="btn"
              disabled={busy || (query === null && maxN === null)}
              onClick={() => run(() => api.send("PUT", "/api/settings", { ...(query !== null ? { gmail_query: query } : {}), ...(maxN !== null ? { gmail_max_messages: maxN } : {}) }), "Saved").then(() => { setQuery(null); setMaxN(null); })}
            >
              Save search
            </button>
          </div>
        </div>
      </Card>
    </section>
  );
}

/** Import page: sync contract notes from Gmail into the normal review flow. */
export function EmailSyncCard() {
  const nav = useNavigate();
  const { data: st } = useAsync(() => api.get<EmailStatus>("/api/email/status"), []);
  const settings = useAsync(() => api.get<{ has_pdf_password: boolean }>("/api/settings"), []);
  const [query, setQuery] = useState<string | null>(null);
  const [onlyNew, setOnlyNew] = useState<boolean | null>(null);
  const [pw, setPw] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [info, setInfo] = useState<SyncResult["summary"] | null>(null);
  if (!st) return null;
  if (!st.connected)
    return (
      <Card title={<span className="row"><Icon name="upload" /> Sync from email</span>}>
        <div className="row">
          <span className="muted">Connect Gmail once and contract notes can be fetched from your inbox.</span>
          <button className="btn sm" onClick={() => nav("/settings#email")}>
            Set up in Settings →
          </button>
        </div>
      </Card>
    );
  const sync = async () => {
    setBusy(true);
    setErr("");
    setInfo(null);
    try {
      const r = await api.send<SyncResult>("POST", "/api/email/sync", { query: query ?? st.query, only_new: onlyNew ?? st.only_new, password: pw || undefined, save_password: !!pw });
      if (r.batch) nav(`/import/${r.batch.id}`);
      else setInfo(r.summary);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card title={<span className="row"><Icon name="upload" /> Sync from email</span>} subtitle={`Connected as ${st.account || "your Gmail"}. Downloads the PDF attachments of matching emails and opens them for review, exactly like an upload.`}>
      <div className="stack">
        {err && <Alert tone="neg">{err}</Alert>}
        {info && (
          <Alert tone="info" title="No new contract notes">
            {info.emails_found} email(s) matched “{info.query}”
            {info.emails_skipped_already_synced ? `; ${info.emails_skipped_already_synced} already imported` : ""}
            {info.emails_without_pdf ? `; ${info.emails_without_pdf} without a PDF` : ""}
            {info.too_large.length ? `; too large: ${info.too_large.join(", ")}` : ""}.
          </Alert>
        )}
        <div className="form-grid">
          <label className="field" style={{ gridColumn: "span 2" }}>
            <span>Search</span>
            <input className="input" value={query ?? st.query} onChange={(e) => setQuery(e.target.value)} />
          </label>
          <label className="field">
            <span>PDF password</span>
            <input className="input" type="password" autoComplete="off" value={pw} onChange={(e) => setPw(e.target.value)} placeholder={settings.data?.has_pdf_password ? "Using saved password" : "if protected"} />
          </label>
        </div>
        <div className="row">
          <label className="check small">
            <input type="checkbox" checked={onlyNew ?? st.only_new} onChange={(e) => setOnlyNew(e.target.checked)} />
            Only emails not synced before
          </label>
          <span className="spacer" />
          <button className="btn primary" disabled={busy} onClick={sync}>
            {busy ? "Fetching from Gmail…" : "Sync from email"}
          </button>
        </div>
      </div>
    </Card>
  );
}
