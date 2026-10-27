import { Fragment, useState } from "react";
import { api } from "../api";
import { useApp } from "../App";
import { PerformancePanel } from "../components/Performance";
import { Alert, Card, Kpi, Spinner, Tabs, useAsync } from "../components/ui";
import { money, pct } from "../format";

type Tab = "performance" | "charges";

export default function Insights() {
  const [tab, setTab] = useState<Tab>("performance");
  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Insights</h1>
          <p>How your money has grown, compared with the market, and what trading costs you. Tax planning has its own page.</p>
        </div>
      </div>
      <Tabs<Tab>
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "performance", label: "Performance" },
          { id: "charges", label: "Trading costs" },

        ]}
      />
      {tab === "charges" && <Charges />}
      {tab === "performance" && <PerformancePanel />}
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
