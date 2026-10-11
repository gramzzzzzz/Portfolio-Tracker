import { Fragment, useState } from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api";
import { useApp } from "../App";
import { Alert, Badge, Card, Empty, Kpi, Spinner, Tabs, useAsync } from "../components/ui";
import { compactMoney, date, money, pct, qty, signedMoney, tone } from "../format";

type Tab = "tax" | "charges" | "benchmark";

export default function Insights() {
  const [tab, setTab] = useState<Tab>("tax");
  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Insights</h1>
          <p>Decisions you can act on: tax planning, cost of trading and how you compare with the market.</p>
        </div>
      </div>
      <Tabs<Tab>
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "tax", label: "Capital gains & tax" },
          { id: "charges", label: "Trading costs" },
          { id: "benchmark", label: "vs. benchmark" },
        ]}
      />
      {tab === "tax" && <Tax />}
      {tab === "charges" && <Charges />}
      {tab === "benchmark" && <Benchmark />}
    </div>
  );
}

function Tax() {
  const { live, epoch } = useApp();
  const { data: d, error, loading } = useAsync(() => api.get<any>(`/api/portfolio/tax?live=${live}`), [live, epoch]);
  if (loading && !d) return <Spinner />;
  if (error) return <Alert tone="neg">{error.message}</Alert>;
  const u = d.unrealised;
  return (
    <div className="stack">
      <Alert tone="info">
        Estimates for {d.fy} using STCG {d.rates.stcg}% / LTCG {d.rates.ltcg}% above {money(d.rates.exemption)} (edit in Settings). Holdings held more than 12 months are long term. This is a planning
        aid, not tax advice.
      </Alert>
      <div className="grid g-kpi">
        <Kpi label="Unrealised LT gains (equity)" value={signedMoney(u.lt_equity)} valueClass={tone(u.lt_equity)} />
        <Kpi label="Unrealised ST gains (equity)" value={signedMoney(u.st_equity)} valueClass={tone(u.st_equity)} />
        {(u.st_other || u.lt_other) ? <Kpi label="Gold / other (ST · LT)" value={`${compactMoney(u.st_other)} · ${compactMoney(u.lt_other)}`} /> : null}
        <Kpi label="LTCG exemption left this FY" value={money(d.exemption_left)} sub={`${d.days_left_in_fy} days left in ${d.fy}`} />
        <Kpi label="Tax if everything sold today" value={money(d.estimated_tax_if_sold_today)} hint="After set-off of losses and the LTCG exemption" />
      </div>

      <Card
        title="Tax-free LTCG harvesting"
        subtitle={`Sell and immediately re-buy to reset your cost basis, using up to ${money(d.harvest.headroom)} of this year's exemption. Units are taken oldest-first (FIFO), as the depository does.`}
        pad={false}
      >
        {d.harvest.plan.length ? (
          <table className="t">
            <thead>
              <tr>
                <th>Holding</th>
                <th className="num">Units to sell</th>
                <th className="num">At price</th>
                <th className="num">Gain booked</th>
                <th className="num">Future tax saved</th>
                <th className="num">Est. charges</th>
              </tr>
            </thead>
            <tbody>
              {d.harvest.plan.map((p: any) => (
                <tr key={p.name}>
                  <td style={{ fontWeight: 600 }}>{p.name}</td>
                  <td className="num">{qty(p.units)}</td>
                  <td className="num">{money(p.price, true)}</td>
                  <td className="num pos">{money(p.gain)}</td>
                  <td className="num">{money(p.tax_saved)}</td>
                  <td className="num muted">{money(p.est_charges)}</td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <td colSpan={3}>Total</td>
                <td className="num">{money(d.harvest.total_gain)}</td>
                <td className="num">{money(d.harvest.total_tax_saved)}</td>
                <td />
              </tr>
            </tfoot>
          </table>
        ) : (
          <Empty title="Nothing to harvest right now">No long-term lots currently sit on a gain, or the exemption is used up.</Empty>
        )}
      </Card>

      <div className="grid g-2">
        <Card title="Turning long-term soon" subtitle="Waiting before selling these lots lowers the tax rate" pad={false}>
          {d.upcoming_lt.length ? (
            <table className="t">
              <thead>
                <tr>
                  <th>Lot</th>
                  <th className="num">Gain</th>
                  <th>Becomes LT</th>
                  <th className="num">Saved by waiting</th>
                </tr>
              </thead>
              <tbody>
                {d.upcoming_lt.map((l: any) => (
                  <tr key={l.row}>
                    <td>
                      <div style={{ fontWeight: 600 }}>{l.name}</div>
                      <div className="muted small">
                        {qty(l.held_qty)} bought {date(l.date)} · row {l.row}
                      </div>
                    </td>
                    <td className="num pos">{money(l.gain)}</td>
                    <td>
                      {date(l.lt_date)} <Badge tone="warn">{l.days_to_lt}d</Badge>
                    </td>
                    <td className="num">{money(l.tax_saved_by_waiting)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty title="No lots crossing the 12-month mark soon" />
          )}
        </Card>
        <Card title="Lots in loss" subtitle={`Can offset gains: ST ${money(d.loss_total.st)} · LT ${money(d.loss_total.lt)}`} pad={false}>
          {d.loss_lots.length ? (
            <table className="t">
              <thead>
                <tr>
                  <th>Lot</th>
                  <th>Term</th>
                  <th className="num">Loss</th>
                </tr>
              </thead>
              <tbody>
                {d.loss_lots.map((l: any) => (
                  <tr key={l.row}>
                    <td>
                      <div style={{ fontWeight: 600 }}>{l.name}</div>
                      <div className="muted small">
                        {qty(l.held_qty)} @ {money(l.rate, true)} on {date(l.date)}
                      </div>
                    </td>
                    <td>
                      <Badge tone={l.term === "LT" ? "pos" : "warn"}>{l.term}</Badge>
                    </td>
                    <td className="num neg">
                      {signedMoney(l.gain)} <span className="small">({pct(l.gain_pct, 1, true)})</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty title="No lots are in a loss" />
          )}
        </Card>
      </div>
    </div>
  );
}

function Charges() {
  const { epoch } = useApp();
  const { data: d, error, loading } = useAsync(() => api.get<any>("/api/portfolio/charges"), [epoch]);
  if (loading && !d) return <Spinner />;
  if (error) return <Alert tone="neg">{error.message}</Alert>;
  const minBucket = d.by_size.filter((b: any) => b.orders).sort((a: any, b: any) => (b.charges_pct ?? 0) - (a.charges_pct ?? 0))[0];
  return (
    <div className="stack">
      <div className="grid g-kpi">
        <Kpi label="Total charges paid" value={money(d.total, true)} sub={`on ${money(d.gross)} bought`} />
        <Kpi label="Cost per ₹100 invested" value={d.pct != null ? `₹${d.pct.toFixed(3)}` : "—"} hint="Brokerage, STT, stamp duty, exchange & SEBI fees, GST" />
      </div>
      {minBucket && d.by_size.filter((b: any) => b.orders).length > 1 && (
        <Alert tone="info">
          Orders of {minBucket.bucket} cost you the most per rupee ({pct(minBucket.charges_pct, 3)}). Fixed charges such as DP fees and minimum stamp duty weigh more on small orders.
        </Alert>
      )}
      <div className="grid g-2">
        <Card title="By order size" pad={false}>
          <table className="t">
            <thead>
              <tr>
                <th>Order value</th>
                <th className="num">Orders</th>
                <th className="num">Charges</th>
                <th className="num">% of value</th>
              </tr>
            </thead>
            <tbody>
              {d.by_size.map((b: any) => (
                <tr key={b.bucket}>
                  <td>{b.bucket}</td>
                  <td className="num">{b.orders}</td>
                  <td className="num">{money(b.charges, true)}</td>
                  <td className="num">{pct(b.charges_pct, 3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
        <Card title="By financial year" pad={false}>
          <table className="t">
            <thead>
              <tr>
                <th>Year</th>
                <th className="num">Bought</th>
                <th className="num">Charges</th>
                <th className="num">%</th>
              </tr>
            </thead>
            <tbody>
              {d.by_fy.map((y: any) => (
                <tr key={y.fy}>
                  <td>{y.fy}</td>
                  <td className="num">{money(y.gross)}</td>
                  <td className="num">{money(y.charges, true)}</td>
                  <td className="num">{pct(y.pct, 3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      </div>
      {Object.keys(d.breakdown_imported).length > 0 && (
        <Card title="Where the money went" subtitle="From imported contract notes">
          <dl className="kv" style={{ maxWidth: 420 }}>
            {Object.entries(d.breakdown_imported).map(([k, v]) => (
              <Fragment key={k}>
                <dt>{LABELS[k] || k}</dt>
                <dd>{money(v as number, true)}</dd>
              </Fragment>
            ))}
          </dl>
        </Card>
      )}
    </div>
  );
}
const LABELS: Record<string, string> = { stt: "Securities transaction tax", stamp_duty: "Stamp duty", exchange_txn: "Exchange transaction charges", sebi_fees: "SEBI turnover fees", cgst: "CGST", sgst: "SGST", igst: "IGST", brokerage: "Brokerage", utt: "UTT", other: "Other" };

function Benchmark() {
  const { epoch } = useApp();
  const { data: d, error, loading } = useAsync(() => api.get<any>("/api/portfolio/benchmark"), [epoch]);
  if (loading && !d) return <Spinner label="Fetching index history" />;
  if (error) return <Alert tone="neg">{error.message}</Alert>;
  if (!d.available) return <Alert tone="info">{d.reason}. Enable live prices in Settings to compare against an index.</Alert>;
  const beat = d.alpha != null && d.alpha >= 0;
  return (
    <div className="stack">
      <Alert tone={beat ? "pos" : "warn"}>
        If every rupee you invested had gone into <strong>{d.symbol}</strong> on the same dates, it would be worth <strong>{money(d.benchmark_value)}</strong> today versus your{" "}
        <strong>{money(d.portfolio_value)}</strong>.
      </Alert>
      <div className="grid g-kpi">
        <Kpi label="Your XIRR" value={pct(d.portfolio_xirr, 1, true)} valueClass={tone(d.portfolio_xirr)} />
        <Kpi label={`${d.symbol} XIRR (same cash flows)`} value={pct(d.benchmark_xirr, 1, true)} valueClass={tone(d.benchmark_xirr)} />
        <Kpi label="Out/under-performance" value={pct(d.alpha, 1, true)} valueClass={tone(d.alpha)} sub="per year" />
      </div>
      <Card title="Growth of your contributions">
        <div style={{ height: 300 }}>
          <ResponsiveContainer>
            <LineChart data={d.series} margin={{ left: 0, right: 12, top: 8 }}>
              <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="var(--border)" />
              <XAxis dataKey="date" tick={{ fontSize: 11, fill: "var(--muted)" }} />
              <YAxis tickFormatter={(v) => compactMoney(v)} width={64} tick={{ fontSize: 11, fill: "var(--muted)" }} />
              <Tooltip formatter={(v) => money(Number(v))} labelFormatter={(l) => date(String(l))} />
              <Legend />
              <Line dataKey="invested" name="Invested" stroke="#94a3b8" dot={false} strokeDasharray="4 4" />
              <Line dataKey="benchmark" name={d.symbol} stroke="#f59e0b" dot={false} strokeWidth={2} />
              <Line dataKey="portfolio" name="Your portfolio (today)" stroke="#2563eb" strokeWidth={0} dot={{ r: 5 }} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </Card>
    </div>
  );
}
