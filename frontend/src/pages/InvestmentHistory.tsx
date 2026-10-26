import { useMemo, useState, type FormEvent } from "react";
import { Area, AreaChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api";
import { useApp } from "../App";
import { Alert, Badge, Card, Empty, Icon, Kpi, Modal, PALETTE, Spinner, Tabs, useAsync, useToast } from "../components/ui";
import { compactMoney, date, money, qty, signedMoney, tone } from "../format";

interface Ev { date: string | null; type: "BUY" | "SELL" | "BONUS" | "SPLIT"; asset_class: string; name: string; units?: number; unit?: string; price?: number; amount: number; charges?: number; gain?: number | null; ratio?: number; source: string; ref: string; remarks?: string }
interface AssetClassInfo { id: string; label: string; supported: boolean; source: string; events: number }
interface Events { events: Ev[]; asset_classes: AssetClassInfo[]; issues: { severity: string; message: string }[]; first_date: string | null }
interface Holding { name: string; asset_class: string; sector: string | null; units: number; unit?: string; cost: number; lots: number; bonus_units?: number; avg_cost: number | null }
interface Txn { id: string; date: string; type: "SELL" | "BONUS" | "SPLIT"; name: string; qty: number; price: number | null; value: number | null; charges: number; stt: number; ratio: number | null; note_no: string | null; source: string; remarks: string | null; legs?: { row: number | null; origin: string; purchase_date: string; units: number }[]; unmatched_units?: number }
interface Txns { transactions: Txn[]; issues: { severity: string; message: string }[]; securities: string[]; warnings: string[] }

type Tab = "events" | "on" | "txns" | "growth";
const TYPE_TONE: Record<string, "pos" | "neg" | "warn" | "info" | undefined> = { BUY: "info", SELL: "neg", BONUS: "pos", SPLIT: "warn" };

export default function InvestmentHistory() {
  const [tab, setTab] = useState<Tab>("events");
  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Investment history</h1>
          <p>Everything you have ever bought, sold or received, across all your investments, and what you held on any date.</p>
        </div>
      </div>
      <Tabs<Tab>
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "events", label: "All activity" },
          { id: "on", label: "Holdings on a date" },
          { id: "txns", label: "Sales, bonus & splits" },
          { id: "growth", label: "Invested over time" },
        ]}
      />
      {tab === "events" && <EventsTab />}
      {tab === "on" && <OnDate />}
      {tab === "txns" && <TransactionsTab />}
      {tab === "growth" && <Growth />}
    </div>
  );
}

function classLabel(classes: AssetClassInfo[] | undefined, id: string) {
  return classes?.find((c) => c.id === id)?.label || id;
}

// ------------------------------------------------------------------ all activity
function EventsTab() {
  const { epoch } = useApp();
  const { data, error, loading } = useAsync(() => api.get<Events>("/api/history/events"), [epoch]);
  const [cls, setCls] = useState("");
  const [type, setType] = useState("");
  const [q, setQ] = useState("");
  const rows = useMemo(
    () => (data?.events || []).filter((e) => (!cls || e.asset_class === cls) && (!type || e.type === type) && (!q || e.name.toLowerCase().includes(q.toLowerCase()))),
    [data, cls, type, q],
  );
  if (loading && !data) return <Spinner />;
  if (error) return <Alert tone="neg">{error.message}</Alert>;
  if (!data) return null;
  const invested = rows.filter((e) => e.amount < 0).reduce((a, e) => a - e.amount, 0);
  const received = rows.filter((e) => e.amount > 0).reduce((a, e) => a + e.amount, 0);
  return (
    <div className="stack">
      <div className="grid g-kpi">
        {data.asset_classes.map((c) => (
          <div className="card kpi" key={c.id} style={{ opacity: c.supported ? 1 : 0.65 }}>
            <div className="label">{c.label}</div>
            <div className="value">{c.supported ? c.events : "—"}</div>
            <div className="sub">{c.supported ? `${c.events === 1 ? "event" : "events"} · ${c.source}` : "coming soon"}</div>
          </div>
        ))}
      </div>
      {data.issues.map((i, k) => (
        <Alert key={k} tone={i.severity === "error" ? "neg" : "warn"}>
          {i.message}
        </Alert>
      ))}
      <Card
        title="Activity"
        subtitle={`${rows.length} events · put in ${money(invested)} · taken out ${money(received)}`}
        actions={
          <>
            <select className="input sm" value={cls} onChange={(e) => setCls(e.target.value)} aria-label="Asset class">
              <option value="">All investments</option>
              {data.asset_classes.filter((c) => c.supported).map((c) => (
                <option key={c.id} value={c.id}>
                  {c.label}
                </option>
              ))}
            </select>
            <select className="input sm" value={type} onChange={(e) => setType(e.target.value)} aria-label="Type">
              <option value="">All types</option>
              {["BUY", "SELL", "BONUS", "SPLIT"].map((t) => (
                <option key={t}>{t}</option>
              ))}
            </select>
            <input className="input sm" style={{ width: 160 }} placeholder="Search" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search" />
          </>
        }
        pad={false}
      >
        {rows.length ? (
          <div className="table-wrap" style={{ maxHeight: 640 }}>
            <table className="t">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>What</th>
                  <th className="num">Units</th>
                  <th className="num">Price</th>
                  <th className="num">Money in / out</th>
                  <th className="num">Gain</th>
                  <th>Recorded in</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((e, i) => (
                  <tr key={i}>
                    <td className="nowrap">{date(e.date)}</td>
                    <td>
                      <Badge tone={TYPE_TONE[e.type]}>{e.type}</Badge> <strong>{e.name}</strong>
                      <div className="muted small">{classLabel(data.asset_classes, e.asset_class)}</div>
                    </td>
                    <td className="num">{e.type === "SPLIT" ? `×${qty(e.ratio)}` : `${e.units! > 0 ? "+" : ""}${qty(e.units)}${e.unit ? " " + e.unit : ""}`}</td>
                    <td className="num">{e.price ? money(e.price, true) : "—"}</td>
                    <td className={`num ${e.amount < 0 ? "" : e.amount > 0 ? "pos" : "muted"}`}>{e.amount ? (e.amount < 0 ? `− ${money(-e.amount)}` : `+ ${money(e.amount)}`) : "—"}</td>
                    <td className={`num ${tone(e.gain)}`}>{e.gain !== undefined && e.gain !== null ? signedMoney(e.gain) : ""}</td>
                    <td className="small muted">
                      {e.source} · {e.ref}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty title="Nothing matches these filters" />
        )}
      </Card>
    </div>
  );
}

// ------------------------------------------------------------------ holdings on a date
function OnDate() {
  const { epoch } = useApp();
  const today = new Date().toISOString().slice(0, 10);
  const [on, setOn] = useState(today);
  const { data, error, loading } = useAsync(() => api.get<{ date: string; holdings: Holding[]; total_cost: number; by_class: Record<string, number> }>(`/api/history/holdings?on=${on}`), [on, epoch]);
  const ev = useAsync(() => api.get<Events>("/api/history/events"), [epoch]);
  return (
    <div className="stack">
      <Card title="What did I hold on…" subtitle="Replays every purchase, sale, bonus and split up to the end of that day. Amounts are at cost.">
        <div className="row">
          <input className="input" type="date" style={{ maxWidth: 200 }} value={on} max={today} min={ev.data?.first_date || undefined} onChange={(e) => setOn(e.target.value || today)} aria-label="Date" />
          {["1y", "3y", "5y"].map((p) => (
            <button
              key={p}
              className="btn sm"
              onClick={() => {
                const d = new Date();
                d.setFullYear(d.getFullYear() - Number(p[0]));
                setOn(d.toISOString().slice(0, 10));
              }}
            >
              {p} ago
            </button>
          ))}
          <button className="btn sm" onClick={() => setOn(today)}>
            Today
          </button>
        </div>
      </Card>
      {loading && !data ? (
        <Spinner />
      ) : error ? (
        <Alert tone="neg">{error.message}</Alert>
      ) : data ? (
        <>
          <div className="grid g-kpi">
            <Kpi label={`Invested on ${date(data.date)}`} value={money(data.total_cost)} sub={`${data.holdings.length} holdings`} />
            {Object.entries(data.by_class).map(([k, v]) => (
              <Kpi key={k} label={classLabel(ev.data?.asset_classes, k)} value={money(v)} />
            ))}
          </div>
          <Card pad={false}>
            {data.holdings.length ? (
              <table className="t">
                <thead>
                  <tr>
                    <th>Holding</th>
                    <th>Type</th>
                    <th className="num">Units</th>
                    <th className="num">Avg cost</th>
                    <th className="num">Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {data.holdings.map((h) => (
                    <tr key={h.name}>
                      <td>
                        <strong>{h.name}</strong>
                        <div className="muted small">
                          {h.sector || ""}
                          {h.bonus_units ? ` · incl. ${qty(h.bonus_units)} bonus` : ""}
                        </div>
                      </td>
                      <td>{classLabel(ev.data?.asset_classes, h.asset_class)}</td>
                      <td className="num">
                        {qty(h.units)}
                        {h.unit ? ` ${h.unit}` : ""}
                      </td>
                      <td className="num">{money(h.avg_cost, true)}</td>
                      <td className="num">{money(h.cost)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <Empty title="You held nothing on this date" />
            )}
          </Card>
        </>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------------ sales, bonus, splits
const EMPTY = { date: new Date().toISOString().slice(0, 10), type: "SELL" as Txn["type"], name: "", qty: "", price: "", charges: "", stt: "", ratio: "", remarks: "" };

function TransactionsTab() {
  const { epoch, refreshHead } = useApp();
  const toast = useToast();
  const { data, error, loading, reload } = useAsync(() => api.get<Txns>("/api/transactions"), [epoch]);
  const [form, setForm] = useState<typeof EMPTY | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  if (loading && !data) return <Spinner />;
  if (error) return <Alert tone="neg">{error.message}</Alert>;
  if (!data) return null;
  const open = (t?: Txn) => {
    setErr("");
    setEditing(t?.id || null);
    setForm(
      t
        ? { date: t.date, type: t.type, name: t.name, qty: String(t.qty || ""), price: String(t.price ?? ""), charges: String(t.charges || ""), stt: String(t.stt || ""), ratio: String(t.ratio ?? ""), remarks: t.remarks || "" }
        : { ...EMPTY, name: data.securities[0] || "" },
    );
  };
  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (!form) return;
    setBusy(true);
    setErr("");
    const n = (v: string) => (v.trim() === "" ? undefined : Number(v));
    const body = { date: form.date, type: form.type, name: form.name, qty: n(form.qty) ?? 0, price: n(form.price), charges: n(form.charges) ?? 0, stt: n(form.stt) ?? 0, ratio: n(form.ratio), remarks: form.remarks || undefined };
    try {
      await api.send(editing ? "PUT" : "POST", editing ? `/api/transactions/${editing}` : "/api/transactions", body);
      setForm(null);
      await refreshHead();
      reload();
      toast("Saved to the Transactions sheet (new sheet version)");
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const del = async (t: Txn) => {
    if (!confirm(`Delete this ${t.type.toLowerCase()} of ${t.name} on ${date(t.date)}?`)) return;
    try {
      await api.send("DELETE", `/api/transactions/${t.id}`);
      await refreshHead();
      reload();
      toast("Deleted (new sheet version; restore it from Sheet history if needed)");
    } catch (e) {
      toast((e as Error).message, "neg");
    }
  };
  return (
    <div className="stack">
      <Alert tone="info">
        These live in a separate <strong>Transactions</strong> sheet inside your workbook — your main sheet is never changed by them. Sales from contract notes are added here automatically when you import.
        Every change is saved as a new sheet version.
      </Alert>
      {[...data.issues, ...data.warnings.map((w) => ({ severity: "warning", message: w }))].map((i, k) => (
        <Alert key={k} tone={i.severity === "error" ? "neg" : "warn"}>
          {i.message}
        </Alert>
      ))}
      <Card
        title="Sales, bonus shares and splits"
        actions={
          <button className="btn primary sm" onClick={() => open()} disabled={!data.securities.length}>
            + Record
          </button>
        }
        pad={false}
      >
        {data.transactions.length ? (
          <table className="t">
            <thead>
              <tr>
                <th>Date</th>
                <th>What</th>
                <th className="num">Units</th>
                <th className="num">Price</th>
                <th className="num">Value</th>
                <th>Units taken from (oldest first)</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.transactions.map((t) => (
                <tr key={t.id}>
                  <td className="nowrap">{date(t.date)}</td>
                  <td>
                    <Badge tone={TYPE_TONE[t.type]}>{t.type}</Badge> <strong>{t.name}</strong>
                    <div className="muted small">
                      {t.id} · {t.source === "contract_note" ? `contract note ${t.note_no}` : "entered by hand"}
                      {t.remarks ? ` · ${t.remarks}` : ""}
                    </div>
                  </td>
                  <td className="num">{t.type === "SPLIT" ? `1 → ${qty(t.ratio)}` : qty(t.qty)}</td>
                  <td className="num">{t.price ? money(t.price, true) : "—"}</td>
                  <td className="num">{t.value ? money(t.value) : "—"}</td>
                  <td className="small">
                    {t.legs?.map((l, i) => (
                      <div key={i}>
                        {qty(l.units)} {l.origin === "bonus" ? "bonus" : `from row ${l.row}`} (bought {date(l.purchase_date)})
                      </div>
                    ))}
                    {(t.unmatched_units ?? 0) > 0 && <div className="neg">{qty(t.unmatched_units)} more than held</div>}
                  </td>
                  <td className="right nowrap">
                    <button className="btn ghost sm" onClick={() => open(t)}>
                      Edit
                    </button>
                    <button className="btn ghost sm danger" onClick={() => del(t)} aria-label={`Delete ${t.id}`}>
                      <Icon name="x" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty title="Nothing recorded yet">Record a sale, bonus shares you received, or a stock split.</Empty>
        )}
      </Card>
      {form && (
        <Modal
          title={editing ? `Edit ${editing}` : "Record a sale, bonus or split"}
          onClose={() => setForm(null)}
          footer={
            <>
              <button className="btn" onClick={() => setForm(null)}>
                Cancel
              </button>
              <button className="btn primary" form="txn-form" disabled={busy}>
                {busy ? "Saving…" : "Save"}
              </button>
            </>
          }
        >
          <form id="txn-form" className="stack" onSubmit={save}>
            {err && <Alert tone="neg">{err}</Alert>}
            <div className="seg" role="group" aria-label="Type">
              {(["SELL", "BONUS", "SPLIT"] as const).map((t) => (
                <button type="button" key={t} aria-pressed={form.type === t} onClick={() => setForm({ ...form, type: t })}>
                  {t === "SELL" ? "Sale" : t === "BONUS" ? "Bonus shares" : "Split"}
                </button>
              ))}
            </div>
            <div className="form-grid">
              <label className="field">
                <span>Holding</span>
                <select className="input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required>
                  {data.securities.map((s) => (
                    <option key={s}>{s}</option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>{form.type === "SELL" ? "Sale date" : form.type === "BONUS" ? "Allotment date" : "Split date"}</span>
                <input className="input" type="date" value={form.date} max={new Date().toISOString().slice(0, 10)} onChange={(e) => setForm({ ...form, date: e.target.value })} required />
              </label>
              {form.type !== "SPLIT" && (
                <label className="field">
                  <span>{form.type === "SELL" ? "Units sold" : "Bonus shares received"}</span>
                  <input className="input" type="number" min="0" step="any" value={form.qty} onChange={(e) => setForm({ ...form, qty: e.target.value })} required />
                </label>
              )}
              {form.type === "SELL" && (
                <>
                  <label className="field">
                    <span>Sale price per unit (₹)</span>
                    <input className="input" type="number" min="0" step="any" value={form.price} onChange={(e) => setForm({ ...form, price: e.target.value })} required />
                  </label>
                  <label className="field">
                    <span>Charges (₹)</span>
                    <input className="input" type="number" min="0" step="any" value={form.charges} placeholder="brokerage, GST, exchange…" onChange={(e) => setForm({ ...form, charges: e.target.value })} />
                  </label>
                  <label className="field">
                    <span>STT (₹)</span>
                    <input className="input" type="number" min="0" step="any" value={form.stt} onChange={(e) => setForm({ ...form, stt: e.target.value })} />
                  </label>
                </>
              )}
              {form.type === "SPLIT" && (
                <label className="field">
                  <span>New shares for each old share</span>
                  <input className="input" type="number" min="0" step="any" value={form.ratio} placeholder="e.g. 5 for a 1:5 split" onChange={(e) => setForm({ ...form, ratio: e.target.value })} required />
                </label>
              )}
            </div>
            <label className="field">
              <span>Remarks (optional)</span>
              <input className="input" value={form.remarks} maxLength={300} onChange={(e) => setForm({ ...form, remarks: e.target.value })} />
            </label>
            <p className="muted small" style={{ margin: 0 }}>
              {form.type === "SELL"
                ? "Units are taken from your oldest purchases first, as your depository does. The realised gain and its tax appear on the Tax planner automatically."
                : form.type === "BONUS"
                  ? "Bonus shares cost nothing and their holding period starts on the allotment date. Don't also add them in the main sheet's quantity column, or they count twice."
                  : "Every lot's units are multiplied and the cost per unit divided; purchase dates (and so long/short term) don't change."}
            </p>
          </form>
        </Modal>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ growth chart
function Growth() {
  const { epoch } = useApp();
  const { data, error, loading } = useAsync(() => api.get<{ points: Record<string, number | string>[]; classes: string[] }>("/api/history/timeline"), [epoch]);
  const ev = useAsync(() => api.get<Events>("/api/history/events"), [epoch]);
  if (loading && !data) return <Spinner />;
  if (error) return <Alert tone="neg">{error.message}</Alert>;
  if (!data || !data.points.length) return <Empty title="No history yet" />;
  return (
    <Card title="Invested amount over time" subtitle="Cost of what you held at the end of each month, by type of investment">
      <div style={{ height: 340 }}>
        <ResponsiveContainer>
          <AreaChart data={data.points} margin={{ left: 0, right: 12, top: 8 }}>
            <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="var(--border)" />
            <XAxis dataKey="month" tick={{ fontSize: 11, fill: "var(--muted)" }} minTickGap={24} />
            <YAxis tickFormatter={(v) => compactMoney(v)} width={64} tick={{ fontSize: 11, fill: "var(--muted)" }} />
            <Tooltip formatter={(v) => money(Number(v))} />
            <Legend />
            {data.classes.map((c, i) => (
              <Area key={c} type="stepAfter" dataKey={c} name={classLabel(ev.data?.asset_classes, c)} stackId="1" isAnimationActive={false} stroke={PALETTE[i % PALETTE.length]} fill={PALETTE[i % PALETTE.length]} fillOpacity={0.35} />
            ))}
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </Card>
  );
}
