import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api";
import { useApp } from "../App";
import { Alert, Card, Empty, Kpi, Spinner, Tabs, useAsync } from "./ui";
import { compactMoney, date, money, pct, signedMoney, tone } from "../format";
import { useState } from "react";

export interface PeriodRow {
  period: string; from: string; twr_from: string; days: number; start_value: number; end_value: number; net_invested: number; gain: number;
  md_pct: number | null; twr_pct: number | null; benchmark_pct: number | null; twr_annualised_pct: number | null; benchmark_annualised_pct: number | null;
}
export interface Performance {
  available: boolean; reason?: string; series_available?: boolean; series_reason?: string; benchmark_symbol: string;
  summary: { total_pnl: number; unrealised: number; realised: number; total_invested: number; total_withdrawn: number; value: number; simple_return_pct: number | null; md_since_start_pct: number | null; xirr_pct: number | null; twr_since_start_pct?: number | null; days: number; start: string };
  contributors: { name: string; unrealised: number; realised: number; total: number; value: number }[];
  periods?: PeriodRow[];
  max_drawdown?: { pct: number; peak: string | null; trough: string | null } | null;
  benchmark?: { value_same_money: number | null; your_value: number; difference: number | null };
  series?: { date: string; value: number; invested: number; growth: number; benchmark_value: number | null; benchmark_growth: number | null }[];
  missing_history?: string[];
  series_start?: string;
}

const LABEL: Record<string, string> = { "1W": "1 week", "1M": "1 month", "3M": "3 months", "6M": "6 months", FY: "This financial year", "1Y": "1 year", "3Y": "3 years", ALL: "Since you started" };

export function usePerformance() {
  const { epoch } = useApp();
  return useAsync(() => api.get<Performance>("/api/portfolio/performance"), [epoch]);
}

/** Compact card for the dashboard. */
export function PerformanceSummary() {
  const { data: p, loading } = usePerformance();
  if (loading && !p) return null;
  if (!p || !p.available || !p.periods) return null;
  const rows = p.periods.filter((r) => ["1W", "1M", "3M", "FY", "1Y", "ALL"].includes(r.period));
  return (
    <Card title="Returns by period" subtitle="On the money you actually had invested in each period (not annualised)" pad={false}>
      <table className="t">
        <thead>
          <tr>
            <th>Period</th>
            <th className="num">Gain</th>
            <th className="num">Your return</th>
            <th className="num">{p.benchmark_symbol === "^NSEI" ? "Nifty 50" : p.benchmark_symbol}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.period}>
              <td>{LABEL[r.period]}</td>
              <td className={`num ${tone(r.gain)}`}>{signedMoney(r.gain)}</td>
              <td className={`num ${tone(r.md_pct)}`}>{pct(r.md_pct, 2, true)}</td>
              <td className={`num ${tone(r.benchmark_pct)}`}>{pct(r.benchmark_pct, 2, true)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

type View = "value" | "growth";

/** Full view (Insights → Performance). */
export function PerformancePanel() {
  const { data: p, error, loading } = usePerformance();
  const [view, setView] = useState<View>("value");
  if (loading && !p) return <Spinner label="Valuing your portfolio day by day" />;
  if (error) return <Alert tone="neg">{error.message}</Alert>;
  if (!p || !p.available) return <Empty title="No purchases yet" />;
  const s = p.summary;
  const bench = p.benchmark_symbol === "^NSEI" ? "Nifty 50" : p.benchmark_symbol;
  return (
    <div className="stack">
      <div className="grid g-kpi">
        <Kpi label="Total profit / loss" value={signedMoney(s.total_pnl)} valueClass={tone(s.total_pnl)} sub={`${signedMoney(s.unrealised)} on holdings · ${signedMoney(s.realised)} booked`} hint="Unrealised gain on what you hold + gains already booked by selling, before tax" />
        <Kpi
          label="Return on money invested"
          value={pct(s.md_since_start_pct, 2, true)}
          valueClass={tone(s.md_since_start_pct)}
          sub={`since ${date(s.start)} · not annualised`}
          hint="Gain divided by the money you had invested on average (each rupee counted only for the time it was invested)"
        />
        <Kpi label="Growth of your picks" value={pct(s.twr_since_start_pct, 2, true)} valueClass={tone(s.twr_since_start_pct)} sub="time-weighted: ignores when and how much you added" hint="Comparable with an index: what ₹100 invested at the start would have become" />
        <Kpi label="Biggest fall from a peak" value={p.max_drawdown ? pct(p.max_drawdown.pct, 1) : "—"} valueClass={p.max_drawdown && p.max_drawdown.pct < 0 ? "neg" : ""} sub={p.max_drawdown?.peak ? `${date(p.max_drawdown.peak)} → ${date(p.max_drawdown.trough)}` : "max drawdown"} />
        <Kpi label="XIRR" value={pct(s.xirr_pct, 1, true)} valueClass={tone(s.xirr_pct)} sub={s.xirr_pct === null ? `available after a year (${s.days} days so far)` : "annualised, money-weighted"} />
      </div>

      {p.series_available === false && <Alert tone="info">{p.series_reason}. Totals above still work.</Alert>}
      {p.missing_history && p.missing_history.length > 0 && <Alert tone="warn">No price history for {p.missing_history.join(", ")}; valued at their last known rate on past days. Set a price symbol on the Holdings page.</Alert>}

      {p.benchmark && p.benchmark.value_same_money !== null && (
        <Alert tone={(p.benchmark.difference ?? 0) >= 0 ? "pos" : "warn"}>
          If every rupee you invested (and took out) had gone into the <strong>{bench}</strong> on the same dates, it would be worth <strong>{money(p.benchmark.value_same_money)}</strong> today versus your{" "}
          <strong>{money(p.benchmark.your_value)}</strong> — {(p.benchmark.difference ?? 0) >= 0 ? "you are ahead by " : "you are behind by "}
          <strong>{money(Math.abs(p.benchmark.difference ?? 0))}</strong>.
        </Alert>
      )}

      {p.periods && (
        <Card title="Returns by period" subtitle="Your return = gain on the money invested in that period. Growth of picks = time-weighted, comparable with the index. Annualised only for periods of a year or more." pad={false}>
          <div className="table-wrap">
            <table className="t">
              <thead>
                <tr>
                  <th>Period</th>
                  <th className="num">Value at start</th>
                  <th className="num">Added / (taken out)</th>
                  <th className="num">Value now</th>
                  <th className="num">Gain</th>
                  <th className="num">Your return</th>
                  <th className="num">Growth of picks</th>
                  <th className="num">{bench}</th>
                  <th className="num">Per year (picks · {bench})</th>
                </tr>
              </thead>
              <tbody>
                {p.periods.map((r) => (
                  <tr key={r.period}>
                    <td>
                      <strong>{LABEL[r.period]}</strong>
                      <div className="muted small">from {date(r.from)}</div>
                    </td>
                    <td className="num">{money(r.start_value)}</td>
                    <td className="num">{r.net_invested ? signedMoney(r.net_invested) : "—"}</td>
                    <td className="num">{money(r.end_value)}</td>
                    <td className={`num ${tone(r.gain)}`}>{signedMoney(r.gain)}</td>
                    <td className={`num ${tone(r.md_pct)}`}>
                      <strong>{pct(r.md_pct, 2, true)}</strong>
                    </td>
                    <td className={`num ${tone(r.twr_pct)}`}>{pct(r.twr_pct, 2, true)}</td>
                    <td className={`num ${tone(r.benchmark_pct)}`}>{pct(r.benchmark_pct, 2, true)}</td>
                    <td className="num muted">{r.twr_annualised_pct !== null ? `${pct(r.twr_annualised_pct, 1, true)} · ${pct(r.benchmark_annualised_pct, 1, true)}` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {p.series && p.series.length > 1 && (
        <Card
          title={view === "value" ? "Value vs money invested" : `Growth of ₹100: your picks vs ${bench}`}
          actions={<Tabs<View> value={view} onChange={setView} tabs={[{ id: "value", label: "Value" }, { id: "growth", label: "Growth of ₹100" }]} />}
        >
          <div style={{ height: 300 }}>
            <ResponsiveContainer>
              <LineChart data={p.series} margin={{ left: 0, right: 12, top: 8 }}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="var(--border)" />
                <XAxis dataKey="date" tick={{ fontSize: 11, fill: "var(--muted)" }} minTickGap={40} tickFormatter={(d) => date(d)} />
                <YAxis tickFormatter={(v) => (view === "value" ? compactMoney(v) : String(Math.round(v)))} width={64} tick={{ fontSize: 11, fill: "var(--muted)" }} domain={["auto", "auto"]} />
                <Tooltip formatter={(v) => (view === "value" ? money(Number(v)) : `₹${Number(v).toFixed(2)}`)} labelFormatter={(l) => date(String(l))} />
                <Legend />
                {view === "value" ? (
                  <>
                    <Line dataKey="value" name="Your portfolio" stroke="#2563eb" dot={false} strokeWidth={2} isAnimationActive={false} />
                    <Line dataKey="invested" name="Money invested (net)" stroke="#94a3b8" strokeDasharray="4 4" dot={false} type="stepAfter" isAnimationActive={false} />
                    <Line dataKey="benchmark_value" name={`Same money in ${bench}`} stroke="#f59e0b" dot={false} isAnimationActive={false} />
                  </>
                ) : (
                  <>
                    <Line dataKey="growth" name="Your picks" stroke="#2563eb" dot={false} strokeWidth={2} isAnimationActive={false} />
                    <Line dataKey="benchmark_growth" name={bench} stroke="#f59e0b" dot={false} isAnimationActive={false} />
                  </>
                )}
              </LineChart>
            </ResponsiveContainer>
          </div>
        </Card>
      )}

      <Card title="What made and lost money" subtitle="Per holding, including gains already booked by selling (before tax)" pad={false}>
        <table className="t">
          <thead>
            <tr>
              <th>Holding</th>
              <th className="num">On holdings</th>
              <th className="num">Booked</th>
              <th className="num">Total</th>
            </tr>
          </thead>
          <tbody>
            {p.contributors.map((c) => (
              <tr key={c.name}>
                <td>
                  <strong>{c.name}</strong>
                  {c.value === 0 && <span className="muted small"> · sold</span>}
                </td>
                <td className={`num ${tone(c.unrealised)}`}>{signedMoney(c.unrealised)}</td>
                <td className={`num ${tone(c.realised)}`}>{c.realised ? signedMoney(c.realised) : "—"}</td>
                <td className={`num ${tone(c.total)}`}>
                  <strong>{signedMoney(c.total)}</strong>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
      <p className="muted small" style={{ margin: 0 }}>
        Shares and ETFs from your main sheet, valued each trading day at closing prices{p.series_start ? ` (from ${date(p.series_start)})` : ""}. Physical gold is on its own page. All figures before tax — see the Tax planner for after-tax amounts.
      </p>
    </div>
  );
}
