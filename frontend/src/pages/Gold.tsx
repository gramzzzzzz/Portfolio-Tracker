import { useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { useApp } from "../App";
import { Alert, Badge, Card, Empty, Icon, Kpi, Modal, Spinner, useAsync, useToast } from "../components/ui";
import { date, money, pct, qty, signedMoney, tone } from "../format";

interface Step { section: string; label: string; amount: number | null; formula: string | null }
interface Item {
  id: string; date: string; description: string; form: "coin" | "bar" | "jewellery"; purity: string; fineness: number; weight_g: number; fine_weight_g: number;
  amount_paid: number; making_charges: number; gst_paid: number; seller: string | null; remarks: string | null; sold_date: string | null; sold_amount: number | null;
  photos: { id: number; filename: string }[]; lt_date: string; status: "held" | "sold"; days_held: number; term: "LT" | "ST"; days_to_lt?: number;
  rate_per_g?: number | null; market_value?: number | null; buyback_deduction_pct?: number; buyback_deduction?: number; sale_value?: number; gain?: number; gain_pct?: number | null;
  tax_if_sold?: number; net_if_sold?: number; after_tax_gain?: number; rate_pct?: number; paid_per_g?: number; computation?: { steps: Step[]; total: number };
  if_wait?: { date: string; tax: number; saved: number };
}
interface Overview {
  price: {
    source: "live" | "manual" | "last_sync" | null; per_g_24k: number | null; intl_per_g_24k: number | null; usd_oz: number | null; usd_inr: number | null;
    premium_pct: number; premium_per_g_24k: number | null; per_g: Record<string, number>; change_pct?: number | null;
    breakdown: Record<string, { international: number | null; premium: number | null; total: number | null }>;
  };
  items: Item[];
  totals: { items: number; weight_g: number; fine_weight_g: number; invested: number; market_value: number; sale_value: number; gain: number; tax_if_sold: number | null; net_if_sold: number | null; after_tax_gain?: number; computation?: { steps: Step[] } };
  by_form: { form: string; items: number; weight_g: number; invested: number; market_value: number }[];
  settings: Record<string, unknown>;
  realised_this_fy: { id: string; description: string; sold_date: string; sold_amount: number; cost: number; gain: number; term: string }[];
  fy: string;
  notes: string[];
}

const FORM_LABEL = { coin: "Coin", bar: "Bar", jewellery: "Jewellery" };
const blank = () => ({ date: "", description: "", form: "jewellery", purity: "22K", weight_g: "", amount_paid: "", making_charges: "", gst_paid: "", seller: "", sold_date: "", sold_amount: "", remarks: "" });

export default function Gold() {
  const { epoch, refreshHead } = useApp();
  const toast = useToast();
  const { data, error, loading, reload } = useAsync(() => api.get<Overview>("/api/gold"), [epoch]);
  const [form, setForm] = useState<ReturnType<typeof blank> | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [detail, setDetail] = useState<Item | null>(null);
  const [showSold, setShowSold] = useState(false);
  if (loading && !data) return <Spinner label="Fetching the gold price" />;
  if (error) return <Alert tone="neg">{error.message}</Alert>;
  if (!data) return null;
  const { price, totals } = data;
  const held = data.items.filter((i) => i.status === "held");
  const sold = data.items.filter((i) => i.status === "sold");
  const purities = Object.keys(price.per_g || {});
  const open = (i?: Item) => {
    setEditing(i?.id || null);
    setForm(
      i
        ? { date: i.date, description: i.description, form: i.form, purity: i.purity, weight_g: String(i.weight_g), amount_paid: String(i.amount_paid), making_charges: String(i.making_charges || ""), gst_paid: String(i.gst_paid || ""), seller: i.seller || "", sold_date: i.sold_date || "", sold_amount: i.sold_amount != null ? String(i.sold_amount) : "", remarks: i.remarks || "" }
        : blank(),
    );
  };
  const done = async (msg: string) => {
    setForm(null);
    setDetail(null);
    await refreshHead();
    reload();
    toast(msg);
  };

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Physical gold</h1>
          <p>Coins, bars and jewellery you own. Stored in a separate Gold sheet in your workbook — the main sheet is never changed.</p>
        </div>
        <button className="btn primary" onClick={() => open()}>
          + Add gold
        </button>
      </div>

      <Card title="Gold price today" subtitle={priceSubtitle(price)} actions={<Link className="btn sm ghost" to="/settings#gold">Price & tax settings →</Link>}>
        {price.per_g_24k ? (
          <div className="stack">
            {price.intl_per_g_24k !== null && (
              <div className="row" style={{ gap: 24, alignItems: "flex-end" }}>
                <div>
                  <div className="muted small">International (24K)</div>
                  <div style={{ fontSize: 22, fontWeight: 650 }} className="mono">
                    {money(price.intl_per_g_24k, true)}
                    <span className="muted small" style={{ fontWeight: 400 }}>
                      {" "}
                      /g
                    </span>
                  </div>
                  <div className="muted small">
                    US${price.usd_oz?.toLocaleString("en-IN")}/oz × ₹{price.usd_inr} per US$
                    {price.change_pct != null && <span className={tone(price.change_pct)}> · {pct(price.change_pct, 2, true)} today</span>}
                  </div>
                </div>
                <div className="muted" style={{ fontSize: 20, paddingBottom: 18 }}>+</div>
                <div>
                  <div className="muted small">Indian premium {price.source === "live" ? `(${price.premium_pct}%)` : ""}</div>
                  <div style={{ fontSize: 22, fontWeight: 650 }} className="mono">
                    {money(price.premium_per_g_24k, true)}
                  </div>
                  <div className="muted small">{price.source === "manual" ? "difference to your own rate" : "import duty 10% + AIDC 5% (adjustable)"}</div>
                </div>
                <div className="muted" style={{ fontSize: 20, paddingBottom: 18 }}>=</div>
                <div>
                  <div className="muted small">Price used (24K)</div>
                  <div style={{ fontSize: 22, fontWeight: 650 }} className="mono">
                    {money(price.per_g_24k, true)}
                  </div>
                  <div className="muted small">{price.source === "manual" ? "your jeweller's rate" : "Indian market estimate"}</div>
                </div>
              </div>
            )}
            <table className="t">
              <thead>
                <tr>
                  <th>Purity</th>
                  <th className="num">International /g</th>
                  <th className="num">+ Premium /g</th>
                  <th className="num">Price used /g</th>
                  <th className="num">10 g</th>
                </tr>
              </thead>
              <tbody>
                {purities.map((p) => {
                  const b = price.breakdown?.[p];
                  return (
                    <tr key={p}>
                      <td>
                        <strong>{p}</strong>
                      </td>
                      <td className="num">{money(b?.international, true)}</td>
                      <td className="num muted">{b?.premium != null ? `+ ${money(b.premium, true)}` : "—"}</td>
                      <td className="num">
                        <strong>{money(price.per_g[p], true)}</strong>
                      </td>
                      <td className="num">{money(price.per_g[p] * 10)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <Alert tone="warn">No gold price available. Live prices are off or unreachable — enter your jeweller's 24K rate per gram in Settings.</Alert>
        )}
      </Card>

      {held.length === 0 && sold.length === 0 ? (
        <Card>
          <Empty title="No gold recorded yet" action={<button className="btn primary" onClick={() => open()}>Add your first item</button>}>
            Add coins, bars or jewellery with the purity, weight and the amount you paid. Photos are optional.
          </Empty>
        </Card>
      ) : (
        <>
          <div className="grid g-kpi">
            <Kpi label="Gold held" value={`${qty(totals.weight_g)} g`} sub={`${qty(totals.fine_weight_g)} g pure · ${totals.items} items`} />
            <Kpi label="Paid" value={money(totals.invested)} sub="incl. making charges & GST" />
            <Kpi label="Market value" value={money(totals.market_value)} sub="at today's rate" />
            <Kpi label="If sold today (pre-tax)" value={money(totals.sale_value)} sub={signedMoney(totals.gain) + " vs paid"} valueClass={tone(totals.gain)} />
            <Kpi label="In hand after tax" value={money(totals.net_if_sold)} sub={`tax ${money(totals.tax_if_sold)}`} valueClass={tone(totals.after_tax_gain)} />
          </div>

          <Card title="Your gold" pad={false}>
            <div className="table-wrap">
              <table className="t">
                <thead>
                  <tr>
                    <th>Item</th>
                    <th className="num">Weight</th>
                    <th className="num">Paid</th>
                    <th className="num">Market value</th>
                    <th className="num">Sale value</th>
                    <th className="num">Gain</th>
                    <th>Tax status</th>
                    <th className="num">Tax if sold</th>
                    <th className="num">In hand</th>
                  </tr>
                </thead>
                <tbody>
                  {held.map((i) => (
                    <tr key={i.id} className="clickable" tabIndex={0} onClick={() => setDetail(i)} onKeyDown={(e) => e.key === "Enter" && setDetail(i)}>
                      <td>
                        <div className="row" style={{ gap: 10, flexWrap: "nowrap" }}>
                          {i.photos[0] ? (
                            <img src={`/api/gold/photos/${i.photos[0].id}`} alt="" width={40} height={40} style={{ objectFit: "cover", borderRadius: 6, flex: "none" }} />
                          ) : (
                            <span className="badge" style={{ width: 40, height: 40, justifyContent: "center", borderRadius: 6 }} aria-hidden>
                              Au
                            </span>
                          )}
                          <div>
                            <strong>{i.description}</strong>
                            <div className="muted small">
                              {FORM_LABEL[i.form]} · {i.purity} · bought {date(i.date)}
                            </div>
                          </div>
                        </div>
                      </td>
                      <td className="num">{qty(i.weight_g)} g</td>
                      <td className="num">{money(i.amount_paid)}</td>
                      <td className="num">{money(i.market_value)}</td>
                      <td className="num" title={`After ${i.buyback_deduction_pct}% buy-back deduction`}>
                        {money(i.sale_value)}
                      </td>
                      <td className={`num ${tone(i.gain)}`}>
                        {signedMoney(i.gain)}
                        <div className="small">{pct(i.gain_pct, 1, true)}</div>
                      </td>
                      <td>
                        {i.term === "LT" ? <Badge tone="pos">Long term</Badge> : <Badge tone="warn">Short term · LT in {i.days_to_lt}d</Badge>}
                      </td>
                      <td className="num">{money(i.tax_if_sold)}</td>
                      <td className="num">{money(i.net_if_sold)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <div className="grid g-2">
            {totals.computation && (
              <Card title="Tax if you sold all of it today" subtitle={`Added to the ${data.fy} gains you have already booked`}>
                <Calc steps={totals.computation.steps} />
              </Card>
            )}
            <Card title="How these numbers are worked out">
              <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
                {data.notes.map((n) => (
                  <li key={n} style={{ marginBottom: 6 }}>
                    {n}
                  </li>
                ))}
              </ul>
            </Card>
          </div>

          {sold.length > 0 && (
            <Card title={`Sold (${sold.length})`} actions={<button className="btn sm" onClick={() => setShowSold(!showSold)}>{showSold ? "Hide" : "Show"}</button>} pad={false}>
              {showSold && (
                <table className="t">
                  <thead>
                    <tr>
                      <th>Item</th>
                      <th>Sold on</th>
                      <th className="num">Paid</th>
                      <th className="num">Sold for</th>
                      <th className="num">Gain</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {sold.map((i) => (
                      <tr key={i.id}>
                        <td>
                          <strong>{i.description}</strong>
                          <div className="muted small">
                            {qty(i.weight_g)} g {i.purity} · bought {date(i.date)}
                          </div>
                        </td>
                        <td>
                          {date(i.sold_date)} <Badge tone={i.term === "LT" ? "pos" : "warn"}>{i.term}</Badge>
                        </td>
                        <td className="num">{money(i.amount_paid)}</td>
                        <td className="num">{money(i.sold_amount)}</td>
                        <td className={`num ${tone(i.gain)}`}>{signedMoney(i.gain)}</td>
                        <td className="right">
                          <button className="btn ghost sm" onClick={() => open(i)}>
                            Edit
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </Card>
          )}
        </>
      )}

      {detail && <Detail item={detail} onClose={() => setDetail(null)} onEdit={() => open(detail)} onChanged={() => reload()} />}
      {form && <GoldForm form={form} setForm={setForm} editing={editing} purities={purities.length ? purities : ["24K", "22K", "18K", "14K"]} onClose={() => setForm(null)} onDone={done} />}
    </div>
  );
}

function priceSubtitle(p: Overview["price"]) {
  if (p.source === "live") return "Live international price plus the Indian premium; all values on this page use the price on the right";
  if (p.source === "manual") return "Your own 24K rate from Settings";
  if (p.source === "last_sync") return "Live price unavailable — last rate written to the Gold sheet";
  return "No price source";
}

function Calc({ steps }: { steps: Step[] }) {
  return (
    <div className="calc">
      {steps.map((s, i) => (
        <div key={i} className={`calc-row ${s.section === "Total" ? "total" : ""}`}>
          <span className="sec">{s.section}</span>
          <span>
            {s.label}
            {s.formula && <span className="f">{s.formula}</span>}
          </span>
          <span className="mono">{s.amount === null ? "" : money(s.amount, true)}</span>
        </div>
      ))}
    </div>
  );
}

function Detail({ item: i, onClose, onEdit, onChanged }: { item: Item; onClose: () => void; onEdit: () => void; onChanged: () => void }) {
  const toast = useToast();
  const ref = useRef<HTMLInputElement>(null);
  const [photos, setPhotos] = useState(i.photos);
  const [busy, setBusy] = useState(false);
  const upload = async (files: FileList | null) => {
    if (!files) return;
    setBusy(true);
    try {
      for (const f of Array.from(files)) {
        const fd = new FormData();
        fd.append("file", f);
        const r = await api.form<{ id: number }>(`/api/gold/${i.id}/photos`, fd);
        setPhotos((p) => [...p, { id: r.id, filename: f.name }]);
      }
      onChanged();
    } catch (e) {
      toast((e as Error).message, "neg");
    } finally {
      setBusy(false);
    }
  };
  const remove = async (pid: number) => {
    await api.send("DELETE", `/api/gold/photos/${pid}`);
    setPhotos((p) => p.filter((x) => x.id !== pid));
    onChanged();
  };
  return (
    <Modal
      title={i.description}
      onClose={onClose}
      footer={
        <button className="btn" onClick={onEdit}>
          Edit / mark as sold
        </button>
      }
    >
      <div className="stack">
        <div className="row" style={{ alignItems: "flex-start" }}>
          {photos.map((p) => (
            <figure key={p.id} style={{ margin: 0, position: "relative" }}>
              <a href={`/api/gold/photos/${p.id}`} target="_blank" rel="noopener">
                <img src={`/api/gold/photos/${p.id}`} alt={`${i.description} — ${p.filename}`} style={{ width: 120, height: 120, objectFit: "cover", borderRadius: 8, border: "1px solid var(--border)" }} />
              </a>
              <button className="btn sm" style={{ position: "absolute", top: 4, right: 4, padding: "0 6px", height: 24 }} onClick={() => remove(p.id)} aria-label={`Delete photo ${p.filename}`}>
                <Icon name="x" />
              </button>
            </figure>
          ))}
          <button className="btn" style={{ width: 120, height: 120, flexDirection: "column" }} onClick={() => ref.current?.click()} disabled={busy}>
            <Icon name="upload" />
            {busy ? "Uploading…" : "Add photo"}
          </button>
          <input ref={ref} type="file" accept="image/jpeg,image/png,image/webp,image/gif,image/heic" multiple hidden onChange={(e) => upload(e.target.files)} />
        </div>
        <dl className="kv">
          <dt>Form · purity</dt>
          <dd>
            {FORM_LABEL[i.form]} · {i.purity} ({(i.fineness * 1000).toFixed(0)} fine)
          </dd>
          <dt>Weight</dt>
          <dd>
            {qty(i.weight_g)} g ({qty(i.fine_weight_g)} g pure gold)
          </dd>
          <dt>Bought</dt>
          <dd>
            {date(i.date)}
            {i.seller ? ` from ${i.seller}` : ""}
          </dd>
          <dt>Paid</dt>
          <dd>
            {money(i.amount_paid, true)} ({money(i.paid_per_g, true)}/g)
          </dd>
          {(i.making_charges > 0 || i.gst_paid > 0) && (
            <>
              <dt>of which making charges · GST</dt>
              <dd>
                {money(i.making_charges, true)} · {money(i.gst_paid, true)}
              </dd>
            </>
          )}
          <dt>Market value</dt>
          <dd>
            {money(i.market_value, true)} ({money(i.rate_per_g, true)}/g)
          </dd>
          <dt>Buy-back deduction ({i.buyback_deduction_pct}%)</dt>
          <dd>− {money(i.buyback_deduction, true)}</dd>
          <dt>You would receive</dt>
          <dd>{money(i.sale_value, true)}</dd>
          <dt>Gain before tax</dt>
          <dd className={tone(i.gain)}>{signedMoney(i.gain)}</dd>
          <dt>Holding period</dt>
          <dd>
            {i.days_held} days · {i.term === "LT" ? "long term" : `short term until ${date(i.lt_date)}`}
          </dd>
          <dt>Tax if sold today ({i.rate_pct}% + cess)</dt>
          <dd>{money(i.tax_if_sold, true)}</dd>
          <dt>
            <strong>In hand after tax</strong>
          </dt>
          <dd>
            <strong>{money(i.net_if_sold, true)}</strong>
          </dd>
        </dl>
        {i.if_wait && i.if_wait.saved > 0.5 && (
          <Alert tone="info" title={`Waiting until ${date(i.if_wait.date)} saves ${money(i.if_wait.saved)}`}>
            After 24 months the gain is taxed at the long-term rate instead of your slab rate: tax {money(i.if_wait.tax)} instead of {money(i.tax_if_sold)} (at today's price).
          </Alert>
        )}
        {i.computation && (
          <details>
            <summary>Detailed tax calculation</summary>
            <Calc steps={i.computation.steps} />
          </details>
        )}
        {i.remarks && <p className="muted small">{i.remarks}</p>}
      </div>
    </Modal>
  );
}

function GoldForm({ form, setForm, editing, purities, onClose, onDone }: { form: ReturnType<typeof blank>; setForm: (f: ReturnType<typeof blank>) => void; editing: string | null; purities: string[]; onClose: () => void; onDone: (m: string) => void }) {
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [sold, setSold] = useState(!!form.sold_date);
  const set = (k: keyof ReturnType<typeof blank>, v: string) => setForm({ ...form, [k]: v });
  const today = new Date().toISOString().slice(0, 10);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setErr("");
    setBusy(true);
    const n = (v: string) => (v.trim() === "" ? 0 : Number(v));
    const body = {
      date: form.date, description: form.description.trim(), form: form.form, purity: form.purity, weight_g: Number(form.weight_g), amount_paid: Number(form.amount_paid),
      making_charges: n(form.making_charges), gst_paid: n(form.gst_paid), seller: form.seller || null,
      sold_date: sold && form.sold_date ? form.sold_date : null, sold_amount: sold && form.sold_amount ? Number(form.sold_amount) : null, remarks: form.remarks || null,
    };
    try {
      await api.send(editing ? "PUT" : "POST", editing ? `/api/gold/${editing}` : "/api/gold", body);
      onDone(editing ? "Gold item updated" : "Gold item added to the Gold sheet");
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };
  const del = async () => {
    if (!editing || !confirm("Remove this item and its photos?")) return;
    await api.send("DELETE", `/api/gold/${editing}`);
    onDone("Gold item removed");
  };
  return (
    <Modal
      title={editing ? "Edit gold item" : "Add gold"}
      onClose={onClose}
      footer={
        <>
          {editing && (
            <button className="btn danger" onClick={del} style={{ marginRight: "auto" }}>
              Remove
            </button>
          )}
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button className="btn primary" form="gold-form" disabled={busy}>
            {busy ? "Saving…" : "Save"}
          </button>
        </>
      }
    >
      <form id="gold-form" className="stack" onSubmit={submit}>
        {err && <Alert tone="neg">{err}</Alert>}
        <label className="field">
          <span>Description</span>
          <input className="input" value={form.description} onChange={(e) => set("description", e.target.value)} placeholder="e.g. Lakshmi coin, wedding necklace" required maxLength={120} />
        </label>
        <div className="form-grid">
          <label className="field">
            <span>Form</span>
            <select className="input" value={form.form} onChange={(e) => set("form", e.target.value)}>
              <option value="coin">Coin</option>
              <option value="bar">Bar</option>
              <option value="jewellery">Jewellery</option>
            </select>
          </label>
          <label className="field">
            <span>Purity</span>
            <select className="input" value={form.purity} onChange={(e) => set("purity", e.target.value)}>
              {purities.map((p) => (
                <option key={p}>{p}</option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Weight (grams)</span>
            <input className="input" type="number" min="0" step="0.001" value={form.weight_g} onChange={(e) => set("weight_g", e.target.value)} required />
          </label>
          <label className="field">
            <span>Purchase date</span>
            <input className="input" type="date" max={today} value={form.date} onChange={(e) => set("date", e.target.value)} required />
          </label>
        </div>
        <div className="form-grid">
          <label className="field">
            <span>Total amount paid (₹)</span>
            <input className="input" type="number" min="0" step="0.01" value={form.amount_paid} onChange={(e) => set("amount_paid", e.target.value)} required />
            <span className="hint">Everything on the bill, incl. making charges and GST</span>
          </label>
          <label className="field">
            <span>of which making charges (₹)</span>
            <input className="input" type="number" min="0" step="0.01" value={form.making_charges} onChange={(e) => set("making_charges", e.target.value)} />
          </label>
          <label className="field">
            <span>of which GST (₹)</span>
            <input className="input" type="number" min="0" step="0.01" value={form.gst_paid} onChange={(e) => set("gst_paid", e.target.value)} />
          </label>
          <label className="field">
            <span>Bought from</span>
            <input className="input" value={form.seller} onChange={(e) => set("seller", e.target.value)} maxLength={120} />
          </label>
        </div>
        <label className="check">
          <input type="checkbox" checked={sold} onChange={(e) => setSold(e.target.checked)} />I have sold this
        </label>
        {sold && (
          <div className="form-grid">
            <label className="field">
              <span>Sold on</span>
              <input className="input" type="date" max={today} value={form.sold_date} onChange={(e) => set("sold_date", e.target.value)} required />
            </label>
            <label className="field">
              <span>Amount received (₹)</span>
              <input className="input" type="number" min="0" step="0.01" value={form.sold_amount} onChange={(e) => set("sold_amount", e.target.value)} required />
            </label>
          </div>
        )}
        <label className="field">
          <span>Remarks</span>
          <input className="input" value={form.remarks} onChange={(e) => set("remarks", e.target.value)} maxLength={300} />
        </label>
        {!editing && <p className="muted small" style={{ margin: 0 }}>You can add photos after saving, by opening the item.</p>}
      </form>
    </Modal>
  );
}
