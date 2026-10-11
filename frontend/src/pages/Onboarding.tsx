import { useRef, useState } from "react";
import { api } from "../api";
import { useApp } from "../App";
import { Alert, Icon } from "../components/ui";

/** First run: the user uploads the tracking sheet that becomes version 1. */
export default function Onboarding() {
  const { refreshHead } = useApp();
  const [file, setFile] = useState<File | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  const upload = async () => {
    if (!file) return;
    setBusy(true);
    setErr("");
    const fd = new FormData();
    fd.append("file", file);
    try {
      await api.form("/api/versions/upload", fd);
      await refreshHead();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <div className="card" style={{ width: "min(560px,100%)" }}>
        <div className="card-body stack">
          <div>
            <h1>Upload your tracking sheet</h1>
            <p className="muted" style={{ margin: "6px 0 0" }}>
              Your Excel workbook (e.g. <code>Invt.xlsx</code>) is the source of truth. The app only ever adds new rows to it and keeps every
              previous version so you can download or restore any snapshot.
            </p>
          </div>
          {err && <Alert tone="neg">{err}</Alert>}
          <div
            className="dropzone"
            role="button"
            tabIndex={0}
            onClick={() => input.current?.click()}
            onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && input.current?.click()}
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault();
              setFile(e.dataTransfer.files[0] || null);
            }}
          >
            <Icon name="sheet" />
            <div style={{ marginTop: 8, fontWeight: 600 }}>{file ? file.name : "Choose or drop an .xlsx file"}</div>
            <div className="muted small">The workbook is validated before it is stored.</div>
            <input ref={input} type="file" accept=".xlsx" hidden onChange={(e) => setFile(e.target.files?.[0] || null)} />
          </div>
          <button className="btn primary" disabled={!file || busy} onClick={upload}>
            {busy ? "Validating…" : "Use this sheet"}
          </button>
        </div>
      </div>
    </div>
  );
}
