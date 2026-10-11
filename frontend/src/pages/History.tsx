import { useRef, useState } from "react";
import { api, download, type Version } from "../api";
import { useApp } from "../App";
import { Alert, Badge, Card, Icon, Modal, Spinner, useAsync, useToast } from "../components/ui";
import { bytes, date, dateTime, money, qty } from "../format";

const SRC: Record<string, { label: string; tone?: "pos" | "info" | "warn" }> = {
  initial: { label: "Initial", tone: "info" },
  upload: { label: "Manual upload", tone: "warn" },
  import: { label: "Contract note import", tone: "pos" },
  restore: { label: "Restore" },
};

export default function History() {
  const { epoch, refreshHead } = useApp();
  const toast = useToast();
  const { data, error, loading, reload } = useAsync(() => api.get<Version[]>("/api/versions"), [epoch]);
  const [diff, setDiff] = useState<Version | null>(null);
  const [confirm, setConfirm] = useState<Version | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);

  if (loading && !data) return <Spinner />;
  if (error) return <Alert tone="neg">{error.message}</Alert>;
  const versions = data || [];

  const restore = async (v: Version) => {
    const nv = await api.send<Version>("POST", `/api/versions/${v.id}/restore`);
    setConfirm(null);
    await refreshHead();
    reload();
    toast(`Version ${v.id} restored as version ${nv.id}`);
  };

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Sheet history</h1>
          <p>Every change to the tracking sheet is kept as an immutable snapshot. Restoring creates a new version, so nothing is ever lost.</p>
        </div>
        <button className="btn" onClick={() => setUploadOpen(true)}>
          <Icon name="upload" /> Upload edited sheet
        </button>
      </div>
      <Card pad={false}>
        <div className="card-body timeline">
          <div className="tl-line" />
          {versions.map((v) => (
            <div className={`tl-item ${v.is_head ? "head" : ""}`} key={v.id}>
              <div className="tl-dot" />
              <div className="row" style={{ alignItems: "flex-start" }}>
                <div style={{ flex: 1, minWidth: 240 }}>
                  <div className="row" style={{ gap: 8 }}>
                    <strong>Version {v.id}</strong>
                    {v.is_head && <Badge tone="info">Current</Badge>}
                    <Badge tone={SRC[v.source]?.tone}>{SRC[v.source]?.label || v.source}</Badge>
                  </div>
                  <div style={{ marginTop: 4 }}>{v.message}</div>
                  <div className="muted small" style={{ marginTop: 2 }}>
                    {dateTime(v.created_at)} · {v.stats.holdings ?? "?"} holdings · {v.stats.lots ?? "?"} purchase rows
                    {v.stats.lots_delta ? ` (${v.stats.lots_delta > 0 ? "+" : ""}${v.stats.lots_delta})` : ""} · invested {money(v.stats.invested)} · {bytes(v.size)}
                    {v.filename && ` · ${v.filename}`}
                  </div>
                </div>
                <div className="row" style={{ gap: 6 }}>
                  {v.parent_id && (
                    <button className="btn sm" onClick={() => setDiff(v)}>
                      <Icon name="diff" /> Changes
                    </button>
                  )}
                  <button className="btn sm" onClick={() => download(`/api/versions/${v.id}/download`)}>
                    <Icon name="download" /> Download
                  </button>
                  {!v.is_head && (
                    <button className="btn sm" onClick={() => setConfirm(v)}>
                      <Icon name="restore" /> Restore
                    </button>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      </Card>
      {diff && <DiffModal v={diff} onClose={() => setDiff(null)} />}
      {confirm && (
        <Modal
          title={`Restore version ${confirm.id}?`}
          onClose={() => setConfirm(null)}
          footer={
            <>
              <button className="btn" onClick={() => setConfirm(null)}>
                Cancel
              </button>
              <button className="btn primary" onClick={() => restore(confirm)}>
                Restore
              </button>
            </>
          }
        >
          <p style={{ marginTop: 0 }}>
            The sheet will go back to how it was on {dateTime(confirm.created_at)} (“{confirm.message}”). This is saved as a new version — the current version stays in history and can be restored again.
          </p>
        </Modal>
      )}
      {uploadOpen && (
        <UploadModal
          onClose={() => setUploadOpen(false)}
          onDone={async () => {
            setUploadOpen(false);
            await refreshHead();
            reload();
            toast("Uploaded sheet saved as a new version");
          }}
        />
      )}
    </div>
  );
}

function DiffModal({ v, onClose }: { v: Version; onClose: () => void }) {
  const { data, loading, error } = useAsync(() => api.get<any>(`/api/versions/${v.id}/diff`), [v.id]);
  return (
    <Modal title={`Changes in version ${v.id}`} onClose={onClose}>
      {loading ? (
        <Spinner />
      ) : error ? (
        <Alert tone="neg">{error.message}</Alert>
      ) : (
        <div className="stack">
          <div className="muted">
            Compared with version {data.against}. Invested {money(data.invested_before)} → {money(data.invested_after)}.
          </div>
          {data.added_lots.length + data.removed_lots.length === 0 && <Alert tone="info">No purchase rows were added or removed.</Alert>}
          {[
            ["Added purchase rows", data.added_lots, "pos"],
            ["Removed purchase rows", data.removed_lots, "neg"],
          ].map(([t, rows, cls]: any) =>
            rows.length ? (
              <div key={t}>
                <h3 style={{ marginBottom: 6 }}>{t}</h3>
                <table className="t">
                  <thead>
                    <tr>
                      <th>Row</th>
                      <th>Name</th>
                      <th>Date</th>
                      <th className="num">Qty</th>
                      <th className="num">Rate</th>
                      <th className="num">Cost</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((l: any, i: number) => (
                      <tr key={i} className={cls}>
                        <td>{l.row}</td>
                        <td>{l.name}</td>
                        <td>{date(l.date)}</td>
                        <td className="num">{qty(l.qty)}</td>
                        <td className="num">{money(l.rate, true)}</td>
                        <td className="num">{money(l.cost, true)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null,
          )}
          {data.added_summary.length > 0 && <div>New summary rows: {data.added_summary.join(", ")}</div>}
          {data.added_sectors.length > 0 && <div>New sectors: {data.added_sectors.join(", ")}</div>}
        </div>
      )}
    </Modal>
  );
}

function UploadModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLInputElement>(null);
  const go = async () => {
    if (!file) return;
    setBusy(true);
    const fd = new FormData();
    fd.append("file", file);
    if (msg) fd.append("message", msg);
    try {
      await api.form("/api/versions/upload", fd);
      onDone();
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };
  return (
    <Modal
      title="Upload an edited sheet"
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button className="btn primary" disabled={!file || busy} onClick={go}>
            {busy ? "Validating…" : "Save as new version"}
          </button>
        </>
      }
    >
      <div className="stack">
        <p className="muted" style={{ margin: 0 }}>
          Edited the workbook in Excel (e.g. updated market rates or recorded a sale)? Upload it here and it becomes the current version. The previous version stays in history.
        </p>
        {err && <Alert tone="neg">{err}</Alert>}
        <button className="btn" onClick={() => ref.current?.click()}>
          <Icon name="sheet" /> {file ? file.name : "Choose .xlsx file"}
        </button>
        <input ref={ref} type="file" accept=".xlsx" hidden onChange={(e) => setFile(e.target.files?.[0] || null)} />
        <label className="field">
          <span>Note (optional)</span>
          <input className="input" value={msg} maxLength={300} onChange={(e) => setMsg(e.target.value)} placeholder="e.g. Updated market rates for October" />
        </label>
      </div>
    </Modal>
  );
}
