import { Fragment, useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { Bar, CartesianGrid, ComposedChart, Legend, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Link } from "react-router-dom";
import { api } from "../api";
import { useApp } from "../App";
import { Alert, Badge, Card, Empty, Kpi, Spinner, Tabs, useAsync, useToast } from "../components/ui";
import { compactMoney, date, money, pct, qty, signedMoney, tone } from "../format";

// ------------------------------------------------------------------ types (API shapes)
type Bucket = "st_equity" | "lt_equity" | "st_slab" | "lt_other";
type GainsT = Record<Bucket, number>;
interface Step { section: string; label: string; amount: number | null; formula: string | null }
interface Computation { total: number; base_total: number; cess: number; surcharge: number; exemption_applied: number; carried_forward: { st: number; lt: number }; steps: Step[]; after_setoff: GainsT; taxable: GainsT }
interface Leg { key: string; name: string; date: string; units: number; price: number; proceeds: number; cost: number; expenses: number; stt: number; gain: number; term: "LT" | "ST"; bucket: Bucket; lt_date: string }
interface Sale { legs: Leg[]; units: number; proceeds: number; expenses: number; stt: number; gain: number; gains: GainsT; tax: number; tax_before: number; tax_after: number; net: number; effective_tax_pct: number | null; computation: Computation }
interface Summary {
  fy: string; fy_end: string; days_left_in_fy: number; today: string;
  rules: { stcg_equity_pct: number; ltcg_pct: number; ltcg_exemption: number; slab_pct: number; surcharge_pct: number; cess_pct: number };
  costs: { expense_pct: number; expense_source: string; dp_charge: number; stt_share_pct: number; stt_fund_pct: number };
  realised: GainsT & {
    current: boolean; fy: string; manual: GainsT;
    sales: { id: string; date: string; name: string; units: number; price: number; proceeds: number; gain: number; gains: GainsT; source: string; note_no: string | null; legs: { row: number | null; origin: string; purchase_date: string; units: number; cost: number; gain: number; term: string; bucket_label: string }[] }[];
    gold: { id: string; description: string; sold_date: string; sold_amount: number; cost: number; gain: number; term: string }[];
  };
  realised_tax: Computation;
  unrealised: GainsT;
  position: { value: number; cost: number; gain_pre_tax: number; tax_if_sold: number; exit_costs: number; expenses: number; stt: number; after_tax_value: number; after_tax_gain: number; computation: Computation; tax_before: number; tax_after: number };
  holdings: { name: string; units: number; price: number; tax_if_sold: number; exit_costs: number; after_tax_value: number; after_tax_gain: number; lt_units: number; st_units: number }[];
  lots: { row: number; bonus?: boolean; name: string; date: string; units: number; price: number; value: number; cost: number; sheet_cost: number; stt_buy: number; exit_costs: number; gain: number; term: "LT" | "ST"; lt_date: string; days_to_lt: number; tax_class: string; bucket: Bucket; bucket_label: string; rate_pct: number; tax_if_sold: number; after_tax_gain: number; older_units_first: number }[];
  excluded: { row: number; name: string; reason: string }[];
  harvest: { headroom: number; plan: { name: string; units: number; price: number; sell_value: number; gain: number; tax_now: number; future_tax_saved: number; round_trip_costs: number; net_benefit: number }[]; total_gain: number; total_future_tax_saved: number };
  loss_harvest: { name: string; units: number; loss: number; tax_saved_this_fy: number; carried_forward: { st: number; lt: number }; terms: string[] }[];
  upcoming_lt: { row: number; name: string; date: string; units: number; gain: number; becomes_lt: string; days: number; tax_now: number; tax_if_waiting: number; saved_by_waiting: number }[];
  assumptions: string[];
}

const BUCKET: Record<Bucket, string> = { st_equity: "Equity short term", lt_equity: "Equity long term", st_slab: "Short term at slab", lt_other: "Gold/other long term" };
type Tab = "sell" | "cash" | "swp" | "lots" | "harvest";

export default function TaxPlanner() {
  const { epoch } = useApp();
  const { data, error, loading, reload } = useAsync(() => api.get<Summary>("/api/tax/summary"), [epoch]);
  const [tab, setTab] = useState<Tab>("sell");
  if (loading && !data) return <Spinner label="Working out your tax position" />;
  if (error) return <Alert tone="neg">{error.message}</Alert>;
  if (!data) return null;
  const d = data;
  const p = d.position;
  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Tax planner</h1>
          <p>
            Capital gains on your shares and ETFs for {d.fy} at live prices: what you would really keep, how to withdraw money with the least tax, and when waiting pays. {d.days_left_in_fy} days left in the financial year. Physical gold has its own tax view on the <Link to="/gold">Physical gold</Link> page; gold you have sold is included in the gains booked below.
          </p>
        </div>
      </div>

      <div className="grid g-kpi">
        <Kpi label="Market value" value={money(p.value)} sub={`cost ${money(p.cost)}`} />
        <Kpi label="Gain before tax" value={signedMoney(p.gain_pre_tax)} valueClass={tone(p.gain_pre_tax)} sub="after selling charges, excl. STT" />
        <Kpi label="Tax if sold today" value={money(p.tax_if_sold)} sub={p.gain_pre_tax > 0 ? `${pct((p.tax_if_sold / p.gain_pre_tax) * 100, 1)} of the gain` : "incl. cess"} />
        <Kpi label="Cash in hand if sold today" value={money(p.after_tax_value)} sub={`after tax and ${money(p.exit_costs)} STT/charges`} />
        <Kpi label="Gain after tax" value={signedMoney(p.after_tax_gain)} valueClass={tone(p.after_tax_gain)} sub={p.cost ? pct((p.after_tax_gain / p.cost) * 100, 2, true) : undefined} />
      </div>

      <div className="grid g-2">
        <Card title="If you sold everything today" subtitle="Step-by-step capital-gains computation for the year, including gains you have already booked">
          <Waterfall
            rows={[
              ["Sale value at live prices", p.value],
              ["Selling charges (deductible)", -p.expenses],
              ["STT on sale (not deductible)", -p.stt],
              ["Capital-gains tax caused by the sale", -p.tax_if_sold],
            ]}
            total={["Cash in hand", p.after_tax_value]}
          />
          <details style={{ marginTop: 14 }}>
            <summary>
              <strong>Show the detailed calculation</strong> <span className="muted small">(tax for the whole FY {money(p.tax_after)}, of which {money(p.tax_before)} is on gains already booked)</span>
            </summary>
            <Calc c={p.computation} />
          </details>
        </Card>
        <Realised d={d} onSaved={reload} />
      </div>

      <Tabs<Tab>
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "sell", label: "Sell a holding" },
          { id: "cash", label: "Raise cash" },
          { id: "swp", label: "Monthly withdrawals" },
          { id: "harvest", label: "Harvesting & timing" },
          { id: "lots", label: "Lot by lot" },
        ]}
      />
      {tab === "sell" && <SellSim d={d} />}
      {tab === "cash" && <RaiseCash d={d} />}
      {tab === "swp" && <Withdrawals d={d} />}
      {tab === "harvest" && <Harvest d={d} />}
      {tab === "lots" && <Lots d={d} />}

      <details className="card card-pad">
        <summary className="muted">Rules and assumptions used</summary>
        <div className="small" style={{ marginTop: 10 }}>
          <p style={{ marginTop: 0 }}>
            Equity STCG {d.rules.stcg_equity_pct}% · LTCG {d.rules.ltcg_pct}% above {money(d.rules.ltcg_exemption)} a year · gold/other ETFs: short term at your slab ({d.rules.slab_pct}%), long term {d.rules.ltcg_pct}% · surcharge {d.rules.surcharge_pct}% · cess {d.rules.cess_pct}%. Selling charges {d.costs.expense_pct}% of value (from {d.costs.expense_source}) + DP {money(d.costs.dp_charge)} per holding; STT on sale {d.costs.stt_share_pct}% for shares, {d.costs.stt_fund_pct}% for equity ETFs/funds, 0 for gold ETFs. Change these in Settings.
          </p>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {d.assumptions.map((a) => (
              <li key={a}>{a}</li>
            ))}
          </ul>
          {d.excluded.length > 0 && <p>Not included (fix in the sheet): {d.excluded.map((e) => `${e.name} row ${e.row} — ${e.reason}`).join("; ")}</p>}
        </div>
      </details>
    </div>
  );
}

// ------------------------------------------------------------------ shared bits
function Waterfall({ rows, total }: { rows: [string, number][]; total: [string, number] }) {
  return (
    <div className="waterfall">
      {rows
        .filter(([, v]) => Math.abs(v) >= 0.5)
        .map(([l, v]) => (
          <div className="wf-row" key={l}>
            <span className={v < 0 ? "muted" : undefined}>{v < 0 ? `− ${l}` : l}</span>
            <span className="mono">{money(Math.abs(v), true)}</span>
          </div>
        ))}
      <div className="wf-row total">
        <span>{total[0]}</span>
        <span className="mono">{money(total[1], true)}</span>
      </div>
    </div>
  );
}

function Calc({ c }: { c: Computation }) {
  return (
    <div className="calc" style={{ marginTop: 10 }}>
      {c.steps.map((s, i) => (
        <div key={i} className={`calc-row ${s.section === "Total" ? "total" : ""}`}>
          <span className="sec">{s.section}</span>
          <span>
            {s.label}
            {s.formula && <span className="f">{s.formula}</span>}
          </span>
          <span className={`mono ${s.amount !== null && s.amount < 0 ? "neg" : ""}`}>{s.amount === null ? "" : money(s.amount, true)}</span>
        </div>
      ))}
    </div>
  );
}

function SaleResult({ s, children }: { s: Sale; children?: ReactNode }) {
  return (
    <div className="stack">
      <div className="grid g-kpi">
        <Kpi label="Sale value (pre-tax)" value={money(s.proceeds)} sub={`${qty(s.units)} units`} />
        <Kpi label="Capital gain" value={signedMoney(s.gain)} valueClass={tone(s.gain)} sub={Object.entries(s.gains).filter(([, v]) => Math.abs(v) >= 0.5).map(([k, v]) => `${BUCKET[k as Bucket]} ${compactMoney(v)}`).join(" · ") || "—"} />
        <Kpi label="Tax on this sale" value={money(s.tax)} sub={s.effective_tax_pct !== null ? `${pct(s.effective_tax_pct, 1)} of the gain` : s.tax < 0 ? "this sale reduces your tax" : undefined} />
        <Kpi label="Cash in hand (after tax)" value={money(s.net)} valueClass="pos" sub={`after ${money(s.expenses + s.stt)} STT & charges`} />
      </div>
      {children}
      <div className="grid g-2">
        <Card title="From sale value to cash in hand">
          <Waterfall
            rows={[
              ["Sale value", s.proceeds],
              ["Selling charges (deductible)", -s.expenses],
              ["STT (not deductible)", -s.stt],
              [s.tax >= 0 ? "Capital-gains tax" : "Tax saved by booking this loss", -s.tax],
            ]}
            total={["Cash in hand", s.net]}
          />
        </Card>
        <Card title="Detailed tax calculation" subtitle={`Whole-FY tax ${money(s.tax_before)} without this sale → ${money(s.tax_after)} with it`}>
          <Calc c={s.computation} />
        </Card>
      </div>
      <Card title="Units sold (oldest first)" pad={false}>
        <LegsTable legs={s.legs} />
      </Card>
    </div>
  );
}

function LegsTable({ legs }: { legs: Leg[] }) {
  return (
    <div className="table-wrap">
      <table className="t">
        <thead>
          <tr>
            <th>Holding / lot</th>
            <th className="num">Units</th>
            <th className="num">Sale value</th>
            <th className="num">Cost (excl. STT)</th>
            <th className="num">Charges</th>
            <th className="num">Gain</th>
            <th>Treated as</th>
          </tr>
        </thead>
        <tbody>
          {legs.map((l, i) => (
            <tr key={i}>
              <td>
                <strong>{l.name}</strong>
                <div className="muted small">
                  {Number(l.key) >= 1000000 ? "bonus shares, allotted" : "bought"} {date(l.date)}{Number(l.key) >= 1000000 ? "" : ` · row ${l.key}`}
                </div>
              </td>
              <td className="num">{qty(l.units)}</td>
              <td className="num">{money(l.proceeds, true)}</td>
              <td className="num">{money(l.cost, true)}</td>
              <td className="num">{money(l.expenses, true)}</td>
              <td className={`num ${tone(l.gain)}`}>{signedMoney(l.gain)}</td>
              <td>
                <Badge tone={l.term === "LT" ? "pos" : "warn"}>{l.term === "LT" ? "Long term" : "Short term"}</Badge> <span className="muted small">{BUCKET[l.bucket]}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function useRun<T>() {
  const [res, setRes] = useState<T | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const run = async (fn: () => Promise<T>) => {
    setBusy(true);
    setErr("");
    try {
      setRes(await fn());
    } catch (e) {
      setErr((e as Error).message);
      setRes(null);
    } finally {
      setBusy(false);
    }
  };
  return { res, err, busy, run };
}

const num = (v: string) => (v.trim() === "" ? undefined : Number(v));

// ------------------------------------------------------------------ gains booked this FY
function Realised({ d, onSaved }: { d: Summary; onSaved: () => void }) {
  const toast = useToast();
  const r = d.realised;
  const init = (k: Bucket) => String(r.current ? r.manual[k] || "" : "");
  const [v, setV] = useState<Record<Bucket, string>>({ st_equity: init("st_equity"), lt_equity: init("lt_equity"), st_slab: init("st_slab"), lt_other: init("lt_other") });
  const [showManual, setShowManual] = useState(Object.values(r.manual).some((x) => x));
  const save = async (e: FormEvent) => {
    e.preventDefault();
    await api.send("PUT", "/api/tax/realised", Object.fromEntries(Object.entries(v).map(([k, x]) => [k, Number(x || 0)])));
    toast("Saved");
    onSaved();
  };
  const total = (Object.keys(BUCKET) as Bucket[]).reduce((a, k) => a + (r[k] || 0), 0);
  return (
    <Card title={`Gains already booked in ${d.fy}`} subtitle="Worked out automatically from the sales you recorded (contract notes, Investment history, Physical gold). Every calculation on this page adds to these, so the exemption and set-off are right.">
      <div className="stack">
        <dl className="kv">
          {(Object.keys(BUCKET) as Bucket[]).map((k) => (
            <Fragment key={k}>
              <dt>{BUCKET[k]}</dt>
              <dd className={tone(r[k])}>{signedMoney(r[k])}</dd>
            </Fragment>
          ))}
          <dt>
            <strong>Total · tax on it so far</strong>
          </dt>
          <dd>
            <strong>
              {signedMoney(total)} · {money(d.realised_tax.total)}
            </strong>
          </dd>
        </dl>
        {r.sales.length + r.gold.length > 0 ? (
          <details>
            <summary className="small">
              {r.sales.length} share/ETF sale{r.sales.length === 1 ? "" : "s"}
              {r.gold.length ? `, ${r.gold.length} gold sale${r.gold.length === 1 ? "" : "s"}` : ""} this year
            </summary>
            <table className="t" style={{ marginTop: 6 }}>
              <tbody>
                {r.sales.map((x) => (
                  <tr key={x.id}>
                    <td className="small">
                      <strong>{x.name}</strong> · {qty(x.units)} @ {money(x.price, true)} on {date(x.date)}
                      <div className="muted">{x.legs.map((l) => `${qty(l.units)} ${l.origin === "bonus" ? "bonus" : "bought " + date(l.purchase_date)} → ${l.bucket_label}`).join("; ")}</div>
                    </td>
                    <td className={`num ${tone(x.gain)}`}>{signedMoney(x.gain)}</td>
                  </tr>
                ))}
                {r.gold.map((x) => (
                  <tr key={x.id}>
                    <td className="small">
                      <strong>{x.description}</strong> (gold) sold {date(x.sold_date)} · {x.term}
                    </td>
                    <td className={`num ${tone(x.gain)}`}>{signedMoney(x.gain)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        ) : (
          <p className="muted small" style={{ margin: 0 }}>No sales recorded this year.</p>
        )}
        <label className="check small">
          <input type="checkbox" checked={showManual} onChange={(e) => setShowManual(e.target.checked)} />I also booked gains outside this app (another broker, sales not recorded here)
        </label>
        {showManual && (
          <form className="stack" onSubmit={save}>
            {!r.current && r.fy && <Alert tone="warn">The extra figures you entered were for {r.fy}; they no longer apply.</Alert>}
            <div className="form-grid">
              {(Object.keys(BUCKET) as Bucket[]).map((k) => (
                <label className="field" key={k}>
                  <span>{BUCKET[k]} (₹)</span>
                  <input className="input" type="number" step="1" value={v[k]} placeholder="0" onChange={(e) => setV({ ...v, [k]: e.target.value })} />
                </label>
              ))}
            </div>
            <div className="row">
              <button className="btn primary">Save</button>
              <span className="muted small">Only gains NOT recorded in this app. Losses as negative numbers.</span>
            </div>
          </form>
        )}
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------ simulator
function SellSim({ d }: { d: Summary }) {
  const [name, setName] = useState(d.holdings[0]?.name || "");
  const [mode, setMode] = useState<"units" | "amount" | "all">("units");
  const [val, setVal] = useState("");
  const [when, setWhen] = useState(d.today);
  const [price, setPrice] = useState("");
  const { res, err, busy, run } = useRun<Sale & { name: string; price: number; held: number; sale_date: string; sheet_cost: number; after_tax_gain: number; if_wait?: { date: string; days: number; tax: number; net: number; saved: number; fy: string } }>();
  const h = d.holdings.find((x) => x.name === name);
  if (!d.holdings.length) return <Empty title="No priced holdings to simulate" />;
  const go = (e: FormEvent) => {
    e.preventDefault();
    run(() =>
      api.send("POST", "/api/tax/simulate-sale", {
        name,
        units: mode === "all" ? h?.units : mode === "units" ? num(val) : undefined,
        amount: mode === "amount" ? num(val) : undefined,
        sale_date: when !== d.today ? when : undefined,
        price: num(price),
      }),
    );
  };
  return (
    <div className="stack">
      <Card title="What would selling leave me with?" subtitle="Units are taken oldest first. Pick a future date to see the effect of lots turning long term or a new financial year.">
        <form className="form-grid" onSubmit={go}>
          <label className="field">
            <span>Holding</span>
            <select className="input" value={name} onChange={(e) => setName(e.target.value)}>
              {d.holdings.map((x) => (
                <option key={x.name}>{x.name}</option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Sell</span>
            <select className="input" value={mode} onChange={(e) => setMode(e.target.value as typeof mode)}>
              <option value="units">a number of units</option>
              <option value="amount">units worth ₹…</option>
              <option value="all">everything</option>
            </select>
          </label>
          {mode !== "all" && (
            <label className="field">
              <span>{mode === "units" ? `Units (of ${qty(h?.units)})` : "Amount (₹)"}</span>
              <input className="input" type="number" min="0" step="any" value={val} onChange={(e) => setVal(e.target.value)} required />
            </label>
          )}
          <label className="field">
            <span>Sale date</span>
            <input className="input" type="date" min={d.today} value={when} onChange={(e) => setWhen(e.target.value)} />
          </label>
          <label className="field">
            <span>Price (optional)</span>
            <input className="input" type="number" min="0" step="any" value={price} placeholder={`live ${h ? h.price : ""}`} onChange={(e) => setPrice(e.target.value)} />
          </label>
          <button className="btn primary" disabled={busy}>
            {busy ? "Calculating…" : "Calculate"}
          </button>
        </form>
        {h && (
          <p className="muted small" style={{ marginBottom: 0 }}>
            {name}: {qty(h.lt_units)} units long term, {qty(h.st_units)} short term today.
          </p>
        )}
      </Card>
      {err && <Alert tone="neg">{err}</Alert>}
      {res && (
        <SaleResult s={res}>
          <Alert tone={res.after_tax_gain >= 0 ? "pos" : "warn"}>
            Selling {qty(res.units)} {res.name} at {money(res.price, true)} on {date(res.sale_date)}: you receive <strong>{money(res.net)}</strong> after tax for units that cost you {money(res.sheet_cost)} — a{" "}
            {res.after_tax_gain >= 0 ? "profit" : "loss"} of <strong>{money(Math.abs(res.after_tax_gain))}</strong> after tax.
          </Alert>
          {res.if_wait && res.if_wait.saved > 0.5 && (
            <Alert tone="info" title={`Waiting ${res.if_wait.days} days saves ${money(res.if_wait.saved)}`}>
              On {date(res.if_wait.date)} every unit sold would be long term: tax {money(res.if_wait.tax)} instead of {money(res.tax)}, cash in hand {money(res.if_wait.net)} (same price
              {res.if_wait.fy !== d.fy ? `, and it falls in ${res.if_wait.fy} with a fresh exemption` : ""}).
            </Alert>
          )}
        </SaleResult>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ raise cash
function Exclude({ d, value, onChange }: { d: Summary; value: string[]; onChange: (v: string[]) => void }) {
  return (
    <details>
      <summary className="small">Holdings to keep (never sell){value.length ? `: ${value.length} selected` : ""}</summary>
      <div className="row" style={{ marginTop: 8 }}>
        {d.holdings.map((h) => (
          <label className="check small" key={h.name}>
            <input type="checkbox" checked={value.includes(h.name)} onChange={(e) => onChange(e.target.checked ? [...value, h.name] : value.filter((x) => x !== h.name))} />
            {h.name}
          </label>
        ))}
      </div>
    </details>
  );
}

function RaiseCash({ d }: { d: Summary }) {
  const [amount, setAmount] = useState("");
  const [exclude, setExclude] = useState<string[]>([]);
  const { res, err, busy, run } = useRun<{ target_net: number; achieved_net: number; shortfall: number; portfolio_value: number; holdings: { name: string; units: number; price: number; proceeds: number; gain: number; gains: GainsT; terms: string[] }[]; sale: Sale }>();
  return (
    <div className="stack">
      <Card title="I need ₹… in hand — what should I sell?" subtitle="Finds the combination that pays the least tax and charges: losses and tax-free long-term gains first, short-term gains last.">
        <form
          className="stack"
          onSubmit={(e) => {
            e.preventDefault();
            run(() => api.send("POST", "/api/tax/raise-cash", { amount: Number(amount), exclude }));
          }}
        >
          <div className="row">
            <input className="input" style={{ maxWidth: 220 }} type="number" min="1" step="any" placeholder="Amount needed (₹)" value={amount} onChange={(e) => setAmount(e.target.value)} required aria-label="Amount needed" />
            <button className="btn primary" disabled={busy}>
              {busy ? "Optimising…" : "Find the cheapest way"}
            </button>
          </div>
          <Exclude d={d} value={exclude} onChange={setExclude} />
        </form>
      </Card>
      {err && <Alert tone="neg">{err}</Alert>}
      {res && (
        <>
          {res.shortfall > 0.5 && <Alert tone="warn">Selling everything allowed raises only {money(res.achieved_net)} — {money(res.shortfall)} short.</Alert>}
          <Card title="Sell these" subtitle={`Raises ${money(res.achieved_net)} in hand; tax ${money(res.sale.tax)}`} pad={false}>
            <table className="t">
              <thead>
                <tr>
                  <th>Holding</th>
                  <th className="num">Units</th>
                  <th className="num">At</th>
                  <th className="num">Sale value</th>
                  <th className="num">Gain</th>
                  <th>Term</th>
                </tr>
              </thead>
              <tbody>
                {res.holdings.map((h) => (
                  <tr key={h.name}>
                    <td style={{ fontWeight: 600 }}>{h.name}</td>
                    <td className="num">{qty(h.units)}</td>
                    <td className="num">{money(h.price, true)}</td>
                    <td className="num">{money(h.proceeds)}</td>
                    <td className={`num ${tone(h.gain)}`}>{signedMoney(h.gain)}</td>
                    <td>{h.terms.map((t) => <Badge key={t} tone={t === "LT" ? "pos" : "warn"}>{t}</Badge>)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
          <SaleResult s={res.sale} />
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ withdrawals
interface Plan {
  rows: { date: string; fy: string; net: number; proceeds: number; tax: number; charges: number; gain: number; st_gain: number; lt_gain: number; sold: { name: string; units: number }[]; value_left: number; shortfall: number }[];
  by_fy: { fy: string; net: number; tax: number; gain: number; lt_gain: number }[];
  months_funded: number; months_requested: number; depleted_on: string | null; total_net: number; total_tax: number; total_charges: number; total_proceeds: number; effective_tax_pct: number | null; value_left: number | null; start: string; starting_value: number;
}

function Withdrawals({ d }: { d: Summary }) {
  const [monthly, setMonthly] = useState("");
  const [months, setMonths] = useState("60");
  const [ret, setRet] = useState("10");
  const [start, setStart] = useState("");
  const [exclude, setExclude] = useState<string[]>([]);
  const [showMonths, setShowMonths] = useState(false);
  const { res, err, busy, run } = useRun<Plan>();
  useEffect(() => setShowMonths(false), [res]);
  const chart = useMemo(() => (res ? res.rows.map((r) => ({ month: r.date.slice(0, 7), value: r.value_left, tax: r.tax, net: r.net })) : []), [res]);
  return (
    <div className="stack">
      <Card title="Monthly withdrawal plan" subtitle="Take a fixed amount in hand every month. Each month the cheapest units to sell are chosen, lots age into long term, prices grow at your expected return and the ₹1.25 L exemption resets every April.">
        <form
          className="form-grid"
          onSubmit={(e) => {
            e.preventDefault();
            run(() => api.send("POST", "/api/tax/withdrawal-plan", { monthly_amount: Number(monthly), months: Number(months), annual_return_pct: Number(ret), start: start ? `${start}-01` : undefined, exclude }));
          }}
        >
          <label className="field">
            <span>Every month, in hand (₹)</span>
            <input className="input" type="number" min="1" step="any" value={monthly} onChange={(e) => setMonthly(e.target.value)} required />
          </label>
          <label className="field">
            <span>For (months)</span>
            <input className="input" type="number" min="1" max="600" value={months} onChange={(e) => setMonths(e.target.value)} required />
          </label>
          <label className="field">
            <span>Expected return (% a year)</span>
            <input className="input" type="number" step="0.5" min="-50" max="50" value={ret} onChange={(e) => setRet(e.target.value)} />
          </label>
          <label className="field">
            <span>Starting month</span>
            <input className="input" type="month" value={start} onChange={(e) => setStart(e.target.value)} />
          </label>
          <button className="btn primary" disabled={busy}>
            {busy ? "Simulating…" : "Simulate"}
          </button>
        </form>
        <div style={{ marginTop: 10 }}>
          <Exclude d={d} value={exclude} onChange={setExclude} />
        </div>
      </Card>
      {err && <Alert tone="neg">{err}</Alert>}
      {res && (
        <>
          <Alert tone={res.depleted_on ? "warn" : "pos"}>
            {res.depleted_on
              ? `The portfolio funds ${res.months_funded} of ${res.months_requested} months; it runs out around ${date(res.depleted_on)}.`
              : `All ${res.months_requested} months are funded, with ${money(res.value_left)} still invested at the end.`}{" "}
            You would pay {money(res.total_tax)} in tax on {money(res.total_proceeds)} sold ({pct(res.effective_tax_pct, 2)} of withdrawals).
          </Alert>
          <div className="grid g-kpi">
            <Kpi label="Received in hand" value={money(res.total_net)} sub={`${res.months_funded} months`} />
            <Kpi label="Total tax" value={money(res.total_tax)} sub={`${pct(res.effective_tax_pct, 2)} of amount sold`} />
            <Kpi label="STT & charges" value={money(res.total_charges)} />
            <Kpi label="Left invested at the end" value={money(res.value_left)} sub={`from ${money(res.starting_value)} today`} />
          </div>
          <Card title="Portfolio value and tax over time">
            <div style={{ height: 260 }}>
              <ResponsiveContainer>
                <ComposedChart data={chart} margin={{ left: 0, right: 8, top: 8 }}>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="var(--border)" />
                  <XAxis dataKey="month" tick={{ fontSize: 11, fill: "var(--muted)" }} minTickGap={24} />
                  <YAxis yAxisId="v" tickFormatter={(v) => compactMoney(v)} width={64} tick={{ fontSize: 11, fill: "var(--muted)" }} />
                  <YAxis yAxisId="t" orientation="right" tickFormatter={(v) => compactMoney(v)} width={56} tick={{ fontSize: 11, fill: "var(--muted)" }} />
                  <Tooltip formatter={(v) => money(Number(v))} />
                  <Legend />
                  <Line yAxisId="v" dataKey="value" name="Still invested" stroke="#2563eb" dot={false} strokeWidth={2} />
                  <Bar yAxisId="t" dataKey="tax" name="Tax that month" fill="#f59e0b" />
                </ComposedChart>
              </ResponsiveContainer>
            </div>
          </Card>
          <Card title="By financial year" pad={false}>
            <table className="t">
              <thead>
                <tr>
                  <th>Year</th>
                  <th className="num">In hand</th>
                  <th className="num">Gains booked</th>
                  <th className="num">of which long term</th>
                  <th className="num">Tax</th>
                </tr>
              </thead>
              <tbody>
                {res.by_fy.map((f) => (
                  <tr key={f.fy}>
                    <td>{f.fy}</td>
                    <td className="num">{money(f.net)}</td>
                    <td className="num">{money(f.gain)}</td>
                    <td className="num">{money(f.lt_gain)}</td>
                    <td className="num">{money(f.tax)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
          <Card title="Month by month" actions={<button className="btn sm" onClick={() => setShowMonths(!showMonths)}>{showMonths ? "Hide" : `Show ${res.rows.length} months`}</button>} pad={false}>
            {showMonths && (
              <div className="table-wrap" style={{ maxHeight: 460 }}>
                <table className="t">
                  <thead>
                    <tr>
                      <th>Month</th>
                      <th>Sell</th>
                      <th className="num">Sale value</th>
                      <th className="num">Gain ST / LT</th>
                      <th className="num">Tax</th>
                      <th className="num">In hand</th>
                      <th className="num">Still invested</th>
                    </tr>
                  </thead>
                  <tbody>
                    {res.rows.map((r) => (
                      <tr key={r.date}>
                        <td className="nowrap">{date(r.date)}</td>
                        <td className="small">{r.sold.map((s) => `${qty(s.units)} ${s.name}`).join(", ")}</td>
                        <td className="num">{money(r.proceeds)}</td>
                        <td className="num">
                          {money(r.st_gain)} / {money(r.lt_gain)}
                        </td>
                        <td className="num">{money(r.tax)}</td>
                        <td className={`num ${r.shortfall > 0.5 ? "neg" : ""}`}>{money(r.net)}</td>
                        <td className="num">{money(r.value_left)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ harvesting & timing
function Harvest({ d }: { d: Summary }) {
  const h = d.harvest;
  return (
    <div className="stack">
      <Card title="Tax-free LTCG booking" subtitle={`Sell and buy back to raise your cost basis using the ${money(h.headroom)} of this year's exemption still unused. Those gains are then never taxed.`} pad={false}>
        {h.plan.length ? (
          <table className="t">
            <thead>
              <tr>
                <th>Holding</th>
                <th className="num">Units</th>
                <th className="num">Gain booked</th>
                <th className="num">Tax now</th>
                <th className="num">Future tax avoided</th>
                <th className="num">Sell + rebuy costs</th>
                <th className="num">Net benefit</th>
              </tr>
            </thead>
            <tbody>
              {h.plan.map((p) => (
                <tr key={p.name}>
                  <td style={{ fontWeight: 600 }}>{p.name}</td>
                  <td className="num">{qty(p.units)}</td>
                  <td className="num pos">{money(p.gain)}</td>
                  <td className="num">{money(p.tax_now)}</td>
                  <td className="num">{money(p.future_tax_saved)}</td>
                  <td className="num muted">{money(p.round_trip_costs)}</td>
                  <td className={`num ${tone(p.net_benefit)}`}>{signedMoney(p.net_benefit)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty title="Nothing to book right now">No long-term equity lot is sitting on a gain, or this year's exemption is used up.</Empty>
        )}
      </Card>
      <div className="grid g-2">
        <Card title="Turning long term soon" subtitle="Selling these after the date shown costs less tax" pad={false}>
          {d.upcoming_lt.length ? (
            <table className="t">
              <thead>
                <tr>
                  <th>Lot</th>
                  <th>Long term from</th>
                  <th className="num">Tax now → then</th>
                  <th className="num">Saved</th>
                </tr>
              </thead>
              <tbody>
                {d.upcoming_lt.map((l) => (
                  <tr key={l.row}>
                    <td>
                      <strong>{l.name}</strong>
                      <div className="muted small">
                        {qty(l.units)} bought {date(l.date)} · gain {money(l.gain)}
                      </div>
                    </td>
                    <td className="nowrap">
                      {date(l.becomes_lt)} <Badge tone="warn">{l.days}d</Badge>
                    </td>
                    <td className="num">
                      {money(l.tax_now)} → {money(l.tax_if_waiting)}
                    </td>
                    <td className="num pos">{money(l.saved_by_waiting)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty title="No lots in profit cross 12 months soon" />
          )}
        </Card>
        <Card title="Losses you could book" subtitle="Oldest units currently at a loss. Booking them offsets gains already booked this year, or carries forward for 8 years." pad={false}>
          {d.loss_harvest.length ? (
            <table className="t">
              <thead>
                <tr>
                  <th>Holding</th>
                  <th className="num">Units</th>
                  <th className="num">Loss</th>
                  <th className="num">Tax saved this FY</th>
                </tr>
              </thead>
              <tbody>
                {d.loss_harvest.map((l) => (
                  <tr key={l.name}>
                    <td>
                      <strong>{l.name}</strong>
                      <div className="muted small">{l.terms.join(" + ")} · carried forward {money(l.carried_forward.st + l.carried_forward.lt)}</div>
                    </td>
                    <td className="num">{qty(l.units)}</td>
                    <td className="num neg">{signedMoney(l.loss)}</td>
                    <td className="num">{money(l.tax_saved_this_fy)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty title="No units are at a loss" />
          )}
        </Card>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ lot by lot
function Lots({ d }: { d: Summary }) {
  return (
    <Card title="Every lot if sold today on its own" subtitle="Tax shown is what that lot alone would add to this year's tax (after your booked gains)." pad={false}>
      <div className="table-wrap">
        <table className="t">
          <thead>
            <tr>
              <th>Lot</th>
              <th className="num">Units</th>
              <th className="num">Value</th>
              <th className="num">Cost (excl. STT)</th>
              <th className="num">Gain</th>
              <th>Status</th>
              <th className="num">Rate</th>
              <th className="num">Tax</th>
              <th className="num">After tax</th>
            </tr>
          </thead>
          <tbody>
            {d.lots.map((l) => (
              <Fragment key={l.row}>
                <tr>
                  <td>
                    <strong>{l.name}</strong>
                    <div className="muted small">
                      {l.bonus ? "bonus shares" : `row ${l.row}`} · {l.bonus ? "allotted" : "bought"} {date(l.date)}
                      {l.older_units_first > 0 && ` · ${qty(l.older_units_first)} older units sell first`}
                    </div>
                  </td>
                  <td className="num">{qty(l.units)}</td>
                  <td className="num">{money(l.value)}</td>
                  <td className="num" title={`Sheet cost ${money(l.sheet_cost, true)} − purchase STT ${money(l.stt_buy, true)}`}>
                    {money(l.cost)}
                  </td>
                  <td className={`num ${tone(l.gain)}`}>{signedMoney(l.gain)}</td>
                  <td>
                    {l.term === "LT" ? <Badge tone="pos">Long term</Badge> : <Badge tone="warn">Short term · LT in {l.days_to_lt}d</Badge>}
                    <div className="muted small">{l.bucket_label}</div>
                  </td>
                  <td className="num">{l.rate_pct}%</td>
                  <td className="num">{money(l.tax_if_sold)}</td>
                  <td className={`num ${tone(l.after_tax_gain)}`}>{signedMoney(l.after_tax_gain)}</td>
                </tr>
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
