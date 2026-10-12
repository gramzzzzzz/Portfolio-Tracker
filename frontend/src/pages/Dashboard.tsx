import { Link } from "react-router-dom";
import { Bar as RBar, BarChart, CartesianGrid, Cell, Line, ComposedChart, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api, type Overview } from "../api";
import { useApp } from "../App";
import { Alert, Badge, Bar, Card, Empty, Kpi, PALETTE, Spinner, useAsync } from "../components/ui";
import { compactMoney, date, money, pct, signedMoney, tone } from "../format";

export default function Dashboard() {
  const { epoch } = useApp();
  const { data, error, loading } = useAsync(() => api.get<Overview>("/api/portfolio/overview"), [epoch]);
  if (loading && !data) return <Spinner label="Reading the sheet" />;
  if (error) return <Alert tone="neg" title="Could not load the portfolio">{error.message}</Alert>;
  if (!data) return null;
  const { kpis, meta, holdings, allocation, concentration, timeline, issues } = data;
  const warnIssues = issues.filter((i) => i.severity !== "info");
  const infoIssues = issues.filter((i) => i.severity === "info");

  if (!holdings.length)
    return (
      <Card>
        <Empty title="No purchases in the sheet yet" action={<Link className="btn primary" to="/import">Import your first contract note</Link>}>
          Upload a broker contract note and the purchase rows will be added to your sheet.
        </Empty>
      </Card>
    );

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Dashboard</h1>
          <p>
            {meta.price_source === "live"
              ? `Valued at live prices${meta.live_coverage !== null && meta.live_coverage < 100 ? ` (${meta.live_coverage}% of holdings; the rest at their last synced price)` : ""}`
              : `Live quotes unavailable right now — valued at the last prices written to the sheet (${date(meta.valuation_date)})`}
          </p>
        </div>
      </div>

      {warnIssues.length > 0 && (
        <Alert tone="warn" title={`${warnIssues.length} thing${warnIssues.length > 1 ? "s" : ""} in the sheet need attention`}>
          <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
            {warnIssues.slice(0, 5).map((i, k) => (
              <li key={k}>
                {i.message} {i.rows.length > 0 && <span className="muted small">(row {i.rows.slice(0, 6).join(", ")})</span>}
              </li>
            ))}
          </ul>
        </Alert>
      )}

      <div className="grid g-kpi">
        <Kpi label="Current value" value={money(kpis.value)} sub={`${kpis.holdings} holdings · ${kpis.lots} purchases`} />
        <Kpi label="Invested" value={money(kpis.invested)} sub={`incl. ${money(kpis.charges)} charges`} />
        <Kpi label="Unrealised P&L (pre-tax)" value={signedMoney(kpis.gain)} valueClass={tone(kpis.gain)} sub={pct(kpis.gain_pct, 2, true)} />
        {kpis.after_tax_gain !== undefined && (
          <Kpi
            label="P&L after tax & exit costs"
            hint="If everything were sold today: STCG/LTCG after set-off and the LTCG exemption, plus cess, STT and selling charges"
            value={signedMoney(kpis.after_tax_gain)}
            valueClass={tone(kpis.after_tax_gain)}
            sub={<Link to="/tax">{`tax ${money(kpis.tax_if_sold)} · costs ${money(kpis.exit_costs)}`}</Link>}
          />
        )}
        <Kpi
          label="XIRR"
          hint="Annualised return that accounts for when each purchase was made"
          value={pct(kpis.xirr, 1, true)}
          valueClass={tone(kpis.xirr)}
          sub={kpis.xirr === null ? "shown once purchases span a year" : "money-weighted, annualised"}
        />
        {kpis.day_change !== null && (
          <Kpi label="Today" value={signedMoney(kpis.day_change)} valueClass={tone(kpis.day_change)} sub={pct(kpis.day_change_pct, 2, true)} />
        )}
      </div>

      <div className="grid g-3">
        <Card title="Top holdings" subtitle="By current value" actions={<Link to="/holdings" className="btn sm ghost">All holdings →</Link>} pad={false}>
          <div className="table-wrap">
            <table className="t">
              <thead>
                <tr>
                  <th>Holding</th>
                  <th className="num">Value</th>
                  <th className="num">P&L</th>
                  <th className="num">XIRR</th>
                  <th style={{ width: 140 }}>Weight</th>
                </tr>
              </thead>
              <tbody>
                {holdings.slice(0, 8).map((h) => (
                  <tr key={h.name}>
                    <td>
                      <div style={{ fontWeight: 600 }}>{h.name}</div>
                      <div className="muted small">{h.sector || "No sector"}</div>
                    </td>
                    <td className="num">{money(h.value)}</td>
                    <td className={`num ${tone(h.gain)}`}>
                      {signedMoney(h.gain)}
                      <div className="small">{pct(h.gain_pct, 1, true)}</div>
                    </td>
                    <td className={`num ${tone(h.xirr)}`} title={h.xirr === null ? "Not annualised: held for less than a year" : undefined}>{pct(h.xirr, 1, true)}</td>
                    <td>
                      <div className="row" style={{ gap: 8, flexWrap: "nowrap" }}>
                        <div style={{ flex: 1 }}>
                          <Bar value={h.weight || 0} max={Math.max(...holdings.map((x) => x.weight || 0))} />
                        </div>
                        <span className="small mono">{pct(h.weight, 1)}</span>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>

        <Card title="Sector allocation" subtitle="Share of current value">
          <div style={{ height: 190 }}>
            <ResponsiveContainer>
              <PieChart>
                <Pie data={allocation.sector} dataKey="value" nameKey="name" innerRadius={55} outerRadius={85} paddingAngle={1} stroke="none">
                  {allocation.sector.map((_, i) => (
                    <Cell key={i} fill={PALETTE[i % PALETTE.length]} />
                  ))}
                </Pie>
                <Tooltip formatter={(v) => money(Number(v))} />
              </PieChart>
            </ResponsiveContainer>
          </div>
          <div className="legend">
            {allocation.sector.map((s, i) => (
              <div className="legend-row" key={s.name}>
                <i style={{ background: PALETTE[i % PALETTE.length] }} />
                <span>{s.name}</span>
                <span className="muted mono">{compactMoney(s.value)}</span>
                <span className="mono" style={{ minWidth: 48, textAlign: "right" }}>{pct(s.weight, 1)}</span>
              </div>
            ))}
          </div>
        </Card>
      </div>

      <div className="grid g-2">
        <Card title="Concentration" subtitle="How diversified the portfolio really is">
          <dl className="kv">
            <dt>Largest holding</dt>
            <dd>{pct(concentration.top1)}</dd>
            <dt>Top 5 holdings</dt>
            <dd>{pct(concentration.top5)}</dd>
            <dt title="1 / Herfindahl index: the number of equally-sized holdings that would give the same concentration">Effective number of holdings</dt>
            <dd>{concentration.effective_n ?? "—"}</dd>
          </dl>
          <div className="stack" style={{ marginTop: 14, gap: 8 }}>
            {concentration.alerts.length === 0 ? (
              <Alert tone="pos">No holding or sector is above your limits.</Alert>
            ) : (
              concentration.alerts.map((a, i) => (
                <Alert tone="warn" key={i}>
                  <strong>{a.name}</strong> is {pct(a.weight)} of the portfolio ({a.type} limit {pct(a.limit, 0)}).
                </Alert>
              ))
            )}
          </div>
        </Card>
        <Card title="Investment timeline" subtitle="Monthly purchases and cumulative cost">
          <div style={{ height: 240 }}>
            <ResponsiveContainer>
              <ComposedChart data={timeline} margin={{ left: 0, right: 8, top: 8 }}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="var(--border)" />
                <XAxis dataKey="month" tick={{ fontSize: 11, fill: "var(--muted)" }} />
                <YAxis yAxisId="l" tickFormatter={(v) => compactMoney(v)} tick={{ fontSize: 11, fill: "var(--muted)" }} width={64} />
                <YAxis yAxisId="r" orientation="right" hide />
                <Tooltip formatter={(v) => money(Number(v))} />
                <RBar yAxisId="l" dataKey="invested" name="Invested in month" fill="#93c5fd" radius={[4, 4, 0, 0]} />
                <Line yAxisId="l" type="monotone" dataKey="cumulative" name="Cumulative" stroke="#2563eb" strokeWidth={2} dot={false} />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </Card>
      </div>

      <Card title="Asset classes" subtitle="Grouped by the sections of your sheet">
        <div style={{ height: Math.max(80, allocation.asset_class.length * 44) }}>
          <ResponsiveContainer>
            <BarChart data={allocation.asset_class} layout="vertical" margin={{ left: 20, right: 30 }}>
              <XAxis type="number" hide />
              <YAxis type="category" dataKey="name" width={110} tick={{ fontSize: 12, fill: "var(--muted)" }} />
              <Tooltip formatter={(v) => money(Number(v))} />
              <RBar dataKey="invested" name="Invested" fill="#cbd5e1" radius={4} />
              <RBar dataKey="value" name="Value" fill="#2563eb" radius={4} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </Card>

      {infoIssues.length > 0 && (
        <details className="card card-pad">
          <summary className="muted">
            Sheet notes <Badge>{infoIssues.length}</Badge>
          </summary>
          <ul style={{ margin: "10px 0 0", paddingLeft: 18 }} className="small">
            {infoIssues.map((i, k) => (
              <li key={k}>{i.message}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
