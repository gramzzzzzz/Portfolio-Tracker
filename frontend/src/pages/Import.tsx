import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, ApiError, download, type Batch, type ImportItem, type PlanResult } from "../api";
import { useApp } from "../App";
import { Alert, Badge, Card, Empty, Icon, Modal, Spinner, useAsync, useToast } from "../components/ui";
import { bytes, date, dateTime, money, qty } from "../format";

export default function ImportPage() {
  const { batchId } = useParams();
  return batchId ? <Review key={batchId} id={batchId} /> : <Upload />;
}

function Steps({ at }: { at: 1 | 2 | 3 }) {
  const s = ["Upload notes", "Review transactions", "Written to sheet"];
  return (
    <div className="steps" aria-label="Progress">
      {s.map((t, i) => (
        <span key={t} className="row" style={{ gap: 8 }}>
          {i > 0 && <span className="step-sep" />}
          <span className={`step ${i + 1 < at ? "done" : i + 1 === at ? "active" : ""}`} aria-current={i + 1 === at ? "step" : undefined}>
            <b>{i + 1 < at ? "✓" : i + 1}</b>
            {t}
          </span>
        </span>
      ))}
    </div>
  );
}

// =================================================================== upload
function Upload() {
  const nav = useNavigate();
  const [files, setFiles] = useState<File[]>([]);
  const [pw, setPw] = useState("");
  const [save, setSave] = useState(true);
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const settings = useAsync(() => api.get<{ has_pdf_password: boolean }>("/api/settings"), []);
  const recent = useAsync(() => api.get<any[]>("/api/imports"), []);
  const hasSaved = settings.data?.has_pdf_password;

  const add = (list: FileList | null) => {
    if (!list) return;
    const pdfs = Array.from(list).filter((f) => f.name.toLowerCase().endsWith(".pdf") || f.type === "application/pdf");
    setFiles((cur) => {
      const names = new Set(cur.map((f) => f.name + f.size));
      return [...cur, ...pdfs.filter((f) => !names.has(f.name + f.size))];
    });
    if (pdfs.length < list.length) setErr("Only PDF files are accepted; other files were ignored.");
  };

  const submit = async () => {
    setBusy(true);
    setErr("");
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    if (pw) fd.append("password", pw);
    fd.append("use_saved_password", "true");
    fd.append("save_password", String(save && !!pw));
    try {
      const b = await api.form<Batch>("/api/imports", fd);
      nav(`/import/${b.id}`);
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Import contract notes</h1>
          <p>Upload one or many broker contract notes. Nothing is written to the sheet until you review and confirm.</p>
        </div>
      </div>
      <Steps at={1} />
      <div className="grid g-3">
        <Card title="Contract notes">
          <div className="stack">
            {err && <Alert tone="neg">{err}</Alert>}
            <div
              className={`dropzone ${over ? "over" : ""}`}
              role="button"
              tabIndex={0}
              aria-label="Choose contract note PDFs"
              onClick={() => input.current?.click()}
              onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && input.current?.click()}
              onDragOver={(e) => {
                e.preventDefault();
                setOver(true);
              }}
              onDragLeave={() => setOver(false)}
              onDrop={(e) => {
                e.preventDefault();
                setOver(false);
                add(e.dataTransfer.files);
              }}
            >
              <Icon name="upload" />
              <div style={{ marginTop: 8, fontWeight: 600 }}>Drop PDFs here or click to choose</div>
              <div className="muted small">Multiple files are fine. All files in one upload should share the same password.</div>
              <input ref={input} type="file" accept="application/pdf,.pdf" multiple hidden onChange={(e) => add(e.target.files)} />
            </div>
            {files.length > 0 && (
              <div className="stack" style={{ gap: 6 }}>
                {files.map((f, i) => (
                  <div className="file-chip" key={f.name + i}>
                    <Icon name="file" />
                    <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis" }}>{f.name}</span>
                    <span className="muted small">{bytes(f.size)}</span>
                    <button className="btn ghost sm" aria-label={`Remove ${f.name}`} onClick={() => setFiles(files.filter((_, j) => j !== i))}>
                      <Icon name="x" />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </Card>
        <Card title="PDF password" subtitle="Contract notes are usually protected (often your PAN)">
          <div className="stack">
            {hasSaved && <Alert tone="info">A saved password will be used automatically. Enter one below only to override it.</Alert>}
            <label className="field">
              <span>Password</span>
              <input className="input" type="password" autoComplete="off" value={pw} onChange={(e) => setPw(e.target.value)} placeholder={hasSaved ? "Using saved password" : "Leave empty if not protected"} />
            </label>
            <label className="check">
              <input type="checkbox" checked={save} onChange={(e) => setSave(e.target.checked)} disabled={!pw} />
              Remember this password (stored encrypted)
            </label>
            <button className="btn primary" disabled={!files.length || busy} onClick={submit}>
              {busy ? "Reading notes…" : `Analyse ${files.length || ""} file${files.length === 1 ? "" : "s"}`}
            </button>
          </div>
        </Card>
      </div>
      <Card title="Recent imports" pad={false}>
        {recent.data && recent.data.length ? (
          <table className="t">
            <thead>
              <tr>
                <th>When</th>
                <th>Files</th>
                <th className="num">Transactions</th>
                <th>Status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {recent.data.slice(0, 10).map((b) => (
                <tr key={b.id}>
                  <td className="nowrap">{dateTime(b.created_at)}</td>
                  <td className="small">{b.files.map((f: any) => f.filename).join(", ")}</td>
                  <td className="num">{b.summary?.trades ?? 0}</td>
                  <td>
                    <Badge tone={b.status === "committed" ? "pos" : b.status === "staged" ? "info" : undefined}>{b.status === "staged" ? "awaiting review" : b.status}</Badge>
                  </td>
                  <td className="right">
                    <Link to={`/import/${b.id}`}>Open</Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty title="No imports yet" />
        )}
      </Card>
    </div>
  );
}

// =================================================================== review
type Dec = { include: boolean; mode: "existing" | "new"; name: string; sector: string; code: string; market_rate: string };

const STATUS: Record<string, { label: string; tone: "pos" | "neg" | "warn" | "info" | undefined }> = {
  new: { label: "New", tone: "info" },
  duplicate: { label: "Already in sheet", tone: undefined },
  possible_duplicate: { label: "Possible duplicate", tone: "warn" },
  sell: { label: "Sell", tone: "neg" },
};

function Review({ id }: { id: string }) {
  const { refreshHead } = useApp();
  const toast = useToast();
  const nav = useNavigate();
  const { data: batch, error, loading, reload } = useAsync(() => api.get<Batch>(`/api/imports/${id}`), [id]);
  const [b, setB] = useState<Batch | null>(null);
  const [dec, setDec] = useState<Record<string, Dec>>({});
  const [extraSectors, setExtraSectors] = useState<string[]>([]);
  const [preview, setPreview] = useState<PlanResult | null>(null);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [pw, setPw] = useState("");

  const load = (x: Batch) => {
    setB(x);
    const d: Record<string, Dec> = {};
    x.items.forEach((it) => {
      const p = it.proposal;
      d[it.id] = {
        include: p.include,
        mode: p.mode,
        name: p.name || "",
        sector: p.sector || "",
        code: p.code || "",
        market_rate: p.market_rate != null ? String(p.market_rate) : "",
      };
    });
    setDec(d);
  };
  useEffect(() => {
    if (batch) load(batch);
  }, [batch]);

  const sectors = useMemo(() => [...new Set([...(b?.sheet.sectors || []), ...extraSectors])], [b, extraSectors]);
  if (loading && !b) return <Spinner label="Reading contract notes" />;
  if (error) return <Alert tone="neg">{error.message}</Alert>;
  if (!b) return null;

  const payload = () => ({
    decisions: b.items.map((it) => {
      const d = dec[it.id];
      return { item_id: it.id, include: d.include, mode: d.mode, name: d.name.trim(), sector: d.sector.trim(), code: d.code.trim() || undefined, market_rate: d.market_rate ? Number(d.market_rate) : undefined };
    }),
  });
  const selected = b.items.filter((it) => dec[it.id]?.include);
  const problems = selected.flatMap((it) => {
    const d = dec[it.id];
    const out: string[] = [];
    if (!d.name.trim()) out.push(`${it.trade.security_name}: enter a name`);
    if (!d.sector.trim()) out.push(`${d.name || it.trade.security_name}: choose a sector`);
    return out;
  });

  const run = async (kind: "preview" | "commit") => {
    setErr("");
    setBusy(kind);
    try {
      if (kind === "preview") {
        setPreview(await api.send<PlanResult>("POST", `/api/imports/${id}/preview`, payload()));
      } else {
        const res = await api.send<Batch>("POST", `/api/imports/${id}/commit`, payload());
        setPreview(null);
        load(res);
        await refreshHead();
        toast(`Sheet updated: ${res.result?.rows.filter((r) => r.kind === "lot").length} purchase row(s) added`);
      }
    } catch (e) {
      const ae = e as ApiError;
      setErr(ae.message);
      setPreview(null);
      if (ae.status === 409) reload();
    } finally {
      setBusy("");
    }
  };

  const retry = async () => {
    setBusy("retry");
    try {
      load(await api.send<Batch>("POST", `/api/imports/${id}/retry`, { password: pw, save_password: true }));
      setPw("");
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy("");
    }
  };

  const set = (iid: string, patch: Partial<Dec>) => setDec((d) => ({ ...d, [iid]: { ...d[iid], ...patch } }));

  if (b.status === "committed" && b.result) return <Done b={b} />;
  if (b.status === "discarded")
    return (
      <Card>
        <Empty title="This import was discarded" action={<Link className="btn" to="/import">Start a new import</Link>} />
      </Card>
    );

  const pwFailed = b.files.filter((f) => f.status === "password_error");
  const byFile = b.files.map((f) => ({ f, items: b.items.filter((i) => i.file_id === f.id) }));

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Review transactions</h1>
          <p>
            {b.summary.trades} transaction{b.summary.trades === 1 ? "" : "s"} found in {b.summary.files_ok} of {b.summary.files} file(s). Every transaction is checked against the rows already in your sheet,
            including ones you typed in manually.
          </p>
        </div>
        <div className="row">
          <button
            className="btn danger"
            onClick={async () => {
              await api.send("DELETE", `/api/imports/${id}`);
              nav("/import");
            }}
          >
            Discard
          </button>
        </div>
      </div>
      <Steps at={2} />

      {b.stale && (
        <Alert tone="warn" title="The sheet changed after these notes were analysed">
          <div className="row" style={{ marginTop: 6 }}>
            Re-check duplicates against the latest version before writing.
            <button className="btn sm" onClick={async () => load(await api.send<Batch>("POST", `/api/imports/${id}/refresh`))}>
              <Icon name="refresh" /> Re-check
            </button>
          </div>
        </Alert>
      )}
      {err && <Alert tone="neg">{err}</Alert>}

      <div className="grid g-kpi">
        <SummaryTile label="New" value={b.summary.new} tone="info" />
        <SummaryTile label="Already in sheet" value={b.summary.duplicates} />
        <SummaryTile label="Possible duplicates" value={b.summary.possible} tone="warn" />
        {b.summary.sells > 0 && <SummaryTile label="Sells (not recorded)" value={b.summary.sells} tone="neg" />}
        {b.summary.files_failed > 0 && <SummaryTile label="Files not read" value={b.summary.files_failed} tone="neg" />}
      </div>

      {pwFailed.length > 0 && (
        <Card title={<span className="row"><Icon name="lock" /> Password needed</span>} subtitle={pwFailed.map((f) => f.filename).join(", ")}>
          <div className="row">
            <input className="input" style={{ maxWidth: 280 }} type="password" placeholder="PDF password" value={pw} onChange={(e) => setPw(e.target.value)} aria-label="PDF password" />
            <button className="btn primary" disabled={!pw || busy === "retry"} onClick={retry}>
              Unlock & read
            </button>
            <span className="muted small">{pwFailed[0].error}</span>
          </div>
        </Card>
      )}

      {byFile.map(({ f, items }) => (
        <Card
          key={f.id}
          title={
            <span className="row">
              <Icon name="file" /> {f.filename}
            </span>
          }
          subtitle={
            f.note
              ? `${f.note.broker} · note ${f.note.note_no} · traded ${date(f.note.trade_date)} · net payable ${money(f.note.net_payable, true)} · charges ${money(f.note.charges_total, true)}`
              : f.error
          }
          actions={<FileBadge status={f.status} />}
          pad={false}
        >
          {f.note?.warnings?.map((w, i) => (
            <div className="card-body" key={i}>
              <Alert tone="warn">{w}</Alert>
            </div>
          ))}
          {items.length > 0 && (
            <div className="table-wrap">
              <table className="t">
                <thead>
                  <tr>
                    <th style={{ width: 36 }}>
                      <span className="sr-only">Include</span>
                    </th>
                    <th>Transaction</th>
                    <th className="num">Qty × rate</th>
                    <th className="num">Charges</th>
                    <th>Check against sheet</th>
                    <th style={{ minWidth: 380 }}>Goes into the sheet as</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((it) => (
                    <ItemRow key={it.id} it={it} d={dec[it.id]} set={(p) => set(it.id, p)} securities={b.sheet.securities} sectors={sectors} sheetSectors={b.sheet.sectors} addSector={(s) => setExtraSectors((x) => [...x, s])} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      ))}

      <div className="card card-pad row" style={{ position: "sticky", bottom: 12, zIndex: 4 }}>
        <div>
          <strong>{selected.length}</strong> transaction{selected.length === 1 ? "" : "s"} selected
          {problems.length > 0 && <div className="small neg">{problems[0]}{problems.length > 1 ? ` (+${problems.length - 1} more)` : ""}</div>}
        </div>
        <span className="spacer" />
        <button className="btn primary" disabled={!selected.length || problems.length > 0 || !!busy || b.stale} onClick={() => run("preview")}>
          {busy === "preview" ? "Planning…" : "Preview changes"}
        </button>
      </div>

      {preview && (
        <Modal
          title="Confirm changes to the sheet"
          onClose={() => setPreview(null)}
          footer={
            <>
              <button className="btn" onClick={() => setPreview(null)}>
                Back
              </button>
              <button className="btn primary" disabled={!!busy || preview.checks.some((c) => !c.passed)} onClick={() => run("commit")}>
                {busy === "commit" ? "Writing…" : "Write to sheet"}
              </button>
            </>
          }
        >
          <Plan plan={preview} />
        </Modal>
      )}
    </div>
  );
}

function SummaryTile({ label, value, tone }: { label: string; value: number; tone?: "info" | "warn" | "neg" }) {
  return (
    <div className="card kpi">
      <div className="label">{label}</div>
      <div className={`value ${tone === "neg" ? "neg" : ""}`} style={{ color: tone === "info" ? "var(--primary)" : tone === "warn" ? "var(--warn)" : undefined }}>
        {value}
      </div>
    </div>
  );
}

function FileBadge({ status }: { status: string }) {
  const m: Record<string, [string, "pos" | "neg" | "warn" | "info" | undefined]> = {
    parsed: ["Read", "pos"],
    password_error: ["Password needed", "warn"],
    error: ["Could not read", "neg"],
    duplicate_file: ["Same note uploaded twice", undefined],
    pending: ["Pending", undefined],
  };
  const [l, t] = m[status] || [status, undefined];
  return <Badge tone={t}>{l}</Badge>;
}

function ItemRow({ it, d, set, securities, sectors, sheetSectors, addSector }: { it: ImportItem; d: Dec; set: (p: Partial<Dec>) => void; securities: string[]; sectors: string[]; sheetSectors: string[]; addSector: (s: string) => void }) {
  const [newSector, setNewSector] = useState(false);
  if (!d) return null;
  const t = it.trade;
  const st = STATUS[it.status];
  const sell = t.side === "SELL";
  return (
    <tr style={{ opacity: d.include || it.status === "new" ? 1 : 0.75 }}>
      <td>
        <input type="checkbox" className="check" checked={d.include} disabled={sell} onChange={(e) => set({ include: e.target.checked })} aria-label={`Include ${t.security_name}`} style={{ width: 16, height: 16 }} />
      </td>
      <td>
        <div style={{ fontWeight: 600 }}>
          {sell ? <Badge tone="neg">SELL</Badge> : <Badge tone="info">BUY</Badge>} {t.security_name}
        </div>
        <div className="muted small">
          {t.isin} · {date(t.trade_date)}
          {t.fills.length > 0 && ` · ${t.fills.length} fill${t.fills.length > 1 ? "s" : ""} on ${[...new Set(t.fills.map((f) => f.exchange))].join("/")}`}
        </div>
      </td>
      <td className="num">
        {qty(t.qty)} × {money(t.rate, true)}
        <div className="muted small">{money(t.gross_value, true)}</div>
      </td>
      <td className="num" title={`STT ${t.stt} · brokerage ${t.brokerage} · GST ${t.gst} · other ${t.other_levies}`}>
        {money(t.charges, true)}
      </td>
      <td style={{ maxWidth: 260 }}>
        <Badge tone={st.tone}>{st.label}</Badge>
        <div className="muted small" style={{ marginTop: 4 }}>
          {it.reason}
        </div>
      </td>
      <td>
        {sell ? (
          <span className="muted small">The tracking sheet has no sell columns; sells are logged in the app only.</span>
        ) : (
          <div className="stack" style={{ gap: 6 }}>
            <div className="row" style={{ gap: 6, flexWrap: "nowrap" }}>
              <select
                className="input sm"
                value={d.mode === "existing" ? d.name : "__new__"}
                onChange={(e) => (e.target.value === "__new__" ? set({ mode: "new", name: it.trade.security_name }) : set({ mode: "existing", name: e.target.value }))}
                aria-label="Holding in sheet"
                disabled={!d.include}
              >
                <option value="__new__">➕ New holding…</option>
                <optgroup label="Existing holdings">
                  {securities.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </optgroup>
              </select>
              {d.mode === "new" && <input className="input sm" value={d.name} onChange={(e) => set({ name: e.target.value })} aria-label="Name in sheet" placeholder="Name in sheet" disabled={!d.include} />}
            </div>
            {d.mode === "new" && (
              <div className="row" style={{ gap: 6, flexWrap: "nowrap" }}>
                {newSector ? (
                  <input
                    className="input sm"
                    autoFocus
                    placeholder="New sector name"
                    value={d.sector}
                    onChange={(e) => set({ sector: e.target.value })}
                    onBlur={() => {
                      if (d.sector.trim()) addSector(d.sector.trim());
                      setNewSector(false);
                    }}
                    aria-label="New sector name"
                  />
                ) : (
                  <select
                    className="input sm"
                    value={d.sector}
                    onChange={(e) => (e.target.value === "__add__" ? (set({ sector: "" }), setNewSector(true)) : set({ sector: e.target.value }))}
                    aria-label="Sector"
                    disabled={!d.include}
                    style={{ borderColor: d.include && !d.sector ? "var(--warn)" : undefined }}
                  >
                    <option value="">Choose sector…</option>
                    {sectors.map((s) => (
                      <option key={s}>{s}</option>
                    ))}
                    <option value="__add__">➕ Add new sector…</option>
                  </select>
                )}
                <input className="input sm" style={{ width: 110 }} value={d.code} onChange={(e) => set({ code: e.target.value })} placeholder="Code" aria-label="Code" disabled={!d.include} />
              </div>
            )}
            {d.mode === "new" && it.proposal.sector_hint && <span className="muted small">Exchange classification: {it.proposal.sector_hint}</span>}
            {d.mode === "new" && d.sector && !sheetSectors.some((x) => x.toLowerCase() === d.sector.trim().toLowerCase()) && (
              <span className="small" style={{ color: "var(--warn)" }}>New sector: it will be added to the Sector table (and a heading if needed). Preview shows the exact rows.</span>
            )}
          </div>
        )}
      </td>
    </tr>
  );
}

function Plan({ plan }: { plan: PlanResult }) {
  const kinds: Record<string, string> = { lot: "Purchase row", block_header: "Block heading", summary: "Summary row", sector: "Sector row", "summary+sector": "Summary + sector row" };
  return (
    <div className="stack">
      <p className="muted" style={{ margin: 0 }}>
        New rows are inserted; existing rows below them shift down (exactly like <em>Insert row</em> in Excel). No existing cell is changed and computed columns get the same formulas as the rows around them.
      </p>
      <table className="t">
        <thead>
          <tr>
            <th>Row</th>
            <th>Type</th>
            <th>What</th>
          </tr>
        </thead>
        <tbody>
          {plan.rows.map((r, i) => (
            <tr key={i}>
              <td className="mono">{r.row}</td>
              <td>
                <Badge tone={r.kind === "lot" ? "info" : undefined}>{kinds[r.kind] || r.kind}</Badge>
              </td>
              <td className="small">{r.description}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3>Safety checks</h3>
      <div className="stack" style={{ gap: 6 }}>
        {plan.checks.map((c) => (
          <div key={c.name} className="row small" style={{ flexWrap: "nowrap" }}>
            <span className={c.passed ? "pos" : "neg"} style={{ width: 16 }}>
              <Icon name={c.passed ? "check" : "x"} />
            </span>
            {c.detail}
          </div>
        ))}
      </div>
      {plan.warnings.map((w, i) => (
        <Alert key={i} tone="warn">
          {w}
        </Alert>
      ))}
    </div>
  );
}

function Done({ b }: { b: Batch }) {
  const v = b.result!.version!;
  const lots = b.result!.rows.filter((r) => r.kind === "lot").length;
  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Sheet updated</h1>
          <p>{v.message}</p>
        </div>
      </div>
      <Steps at={3} />
      <Alert tone="pos" title={`${lots} purchase row${lots === 1 ? "" : "s"} written — saved as sheet version ${v.id}`}>
        The previous version is kept in Sheet history and can be restored at any time.
      </Alert>
      <Card title="What changed">
        <Plan plan={b.result!} />
      </Card>
      <div className="row">
        <button className="btn primary" onClick={() => download(`/api/versions/${v.id}/download`)}>
          <Icon name="download" /> Download updated sheet
        </button>
        <Link className="btn" to="/">
          Go to dashboard
        </Link>
        <Link className="btn" to="/import">
          Import more notes
        </Link>
      </div>
    </div>
  );
}
