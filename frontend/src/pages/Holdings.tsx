import { Fragment, useMemo, useState } from "react";
import { api, type Holding, type Overview } from "../api";
import { useApp } from "../App";
import { Alert, Badge, Card, Icon, Spinner, useAsync, useToast } from "../components/ui";
import { date, money, pct, qty, signedMoney, tone } from "../format";

type Key = "name" | "value" | "invested" | "gain" | "gain_pct" | "xirr" | "weight" | "sector";

export default function Holdings() {
  const { live, epoch } = useApp();
  const { data, error, loading, reload } = useAsync(() => api.get<Overview>(`/api/portfolio/overview?live=${live}`), [live, epoch]);
  const [sort, setSort] = useState<{ key: Key; dir: 1 | -1 }>({ key: "value", dir: -1 });
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [sector, setSector] = useState("");

  const rows = useMemo(() => {
    const hs = (data?.holdings || []).filter((h) => (!q || h.name.toLowerCase().includes(q.toLowerCase())) && (!sector || h.sector === sector));
    return [...hs].sort((a, b) => {
      const av = a[sort.key] ?? -Infinity;
      const bv = b[sort.key] ?? -Infinity;
      return (av < bv ? -1 : av > bv ? 1 : 0) * sort.dir;
    });
  }, [data, q, sort, sector]);

  if (loading && !data) return <Spinner />;
  if (error) return <Alert tone="neg">{error.message}</Alert>;
  if (!data) return null;
  const sectors = [...new Set(data.holdings.map((h) => h.sector || ""))].filter(Boolean).sort();
  const th = (key: Key, label: string, num = true) => (
    <th
      className={`sortable ${num ? "num" : ""}`}
      aria-sort={sort.key === key ? (sort.dir === 1 ? "ascending" : "descending") : "none"}
      onClick={() => setSort((s) => ({ key, dir: s.key === key ? ((-s.dir) as 1 | -1) : num ? -1 : 1 }))}
    >
      {label}
      {sort.key === key ? (sort.dir === 1 ? " ▲" : " ▼") : ""}
    </th>
  );
  const tot = rows.reduce((a, h) => ({ v: a.v + h.value, i: a.i + h.invested }), { v: 0, i: 0 });

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Holdings</h1>
          <p>One line per security, aggregated from every purchase row in the sheet. Click a holding to see its individual lots.</p>
        </div>
        <div className="row">
          <select className="input sm" style={{ width: 160 }} value={sector} onChange={(e) => setSector(e.target.value)} aria-label="Filter by sector">
            <option value="">All sectors</option>
            {sectors.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
          <input className="input sm" style={{ width: 200 }} placeholder="Search holdings" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search holdings" />
        </div>
      </div>
      <Card pad={false}>
        <div className="table-wrap">
          <table className="t">
            <thead>
              <tr>
                {th("name", "Holding", false)}
                {th("sector", "Sector", false)}
                <th className="num">Qty</th>
                <th className="num">Avg cost</th>
                <th className="num">Price</th>
                {th("invested", "Invested")}
                {th("value", "Value")}
                {th("gain", "P&L")}
                {th("xirr", "XIRR")}
                {th("weight", "Weight")}
              </tr>
            </thead>
            <tbody>
              {rows.map((h) => (
                <Fragment key={h.name}>
                  <tr
                    className="clickable"
                    onClick={() => setOpen(open === h.name ? null : h.name)}
                    aria-expanded={open === h.name}
                    tabIndex={0}
                    onKeyDown={(e) => e.key === "Enter" && setOpen(open === h.name ? null : h.name)}
                  >
                    <td>
                      <div style={{ fontWeight: 600 }}>{h.name}</div>
                      <div className="muted small">
                        {h.lots} lot{h.lots > 1 ? "s" : ""} · since {date(h.first_date)}
                        {h.lt_qty > 0 && h.st_qty > 0 && " · mixed LT/ST"}
                      </div>
                    </td>
                    <td>{h.sector || <Badge tone="warn">none</Badge>}</td>
                    <td className="num">{qty(h.qty)}</td>
                    <td className="num">{money(h.avg_cost, true)}</td>
                    <td className="num">
                      {money(h.price, true)}
                      <div className="small">
                        {h.price_source === "live" ? (
                          <span className={tone(h.day_change_pct)}>{pct(h.day_change_pct, 2, true)} today</span>
                        ) : (
                          <span className="muted">sheet rate</span>
                        )}
                      </div>
                    </td>
                    <td className="num">{money(h.invested)}</td>
                    <td className="num">{money(h.value)}</td>
                    <td className={`num ${tone(h.gain)}`}>
                      {signedMoney(h.gain)}
                      <div className="small">{pct(h.gain_pct, 1, true)}</div>
                    </td>
                    <td className={`num ${tone(h.xirr)}`} title={h.xirr === null ? "Not annualised: held for less than a year" : undefined}>{pct(h.xirr, 1, true)}</td>
                    <td className="num">{pct(h.weight, 1)}</td>
                  </tr>
                  {open === h.name && <LotRows h={h} onSymbol={reload} />}
                </Fragment>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <td colSpan={5}>Total ({rows.length})</td>
                <td className="num">{money(tot.i)}</td>
                <td className="num">{money(tot.v)}</td>
                <td className={`num ${tone(tot.v - tot.i)}`}>{signedMoney(tot.v - tot.i)}</td>
                <td colSpan={2} />
              </tr>
            </tfoot>
          </table>
        </div>
      </Card>
    </div>
  );
}

function LotRows({ h, onSymbol }: { h: Holding; onSymbol: () => void }) {
  const toast = useToast();
  const [sym, setSym] = useState(h.symbol || "");
  const save = async () => {
    await api.send("PUT", `/api/securities/${encodeURIComponent(h.name)}/symbol`, { symbol: sym || null });
    toast(`Price symbol for ${h.name} saved`);
    onSymbol();
  };
  return (
    <>
      <tr className="sub">
        <td colSpan={10}>
          <div className="row small">
            <span className="muted">Sheet rows {h.rows.join(", ")}</span>
            {h.high52 && (
              <span className="muted">
                · 52-week range {money(h.low52, true)} – {money(h.high52, true)} ({pct(h.from_high_pct, 1, true)} from high)
              </span>
            )}
            <span className="spacer" />
            <label className="row" style={{ gap: 6 }}>
              <span className="muted">Live price symbol</span>
              <input className="input sm" style={{ width: 140 }} value={sym} placeholder="e.g. ITC.NS" onChange={(e) => setSym(e.target.value.toUpperCase())} onClick={(e) => e.stopPropagation()} />
            </label>
            <button className="btn sm" onClick={save}>
              <Icon name="check" /> Save
            </button>
          </div>
        </td>
      </tr>
      <tr className="sub">
        <td colSpan={10} style={{ padding: 0 }}>
          <table className="t">
            <thead>
              <tr>
                <th>Row</th>
                <th>Bought on</th>
                <th className="num">Qty</th>
                <th className="num">Rate</th>
                <th className="num">Charges</th>
                <th className="num">Cost</th>
                <th className="num">Value</th>
                <th className="num">P&L</th>
                <th>Holding period</th>
              </tr>
            </thead>
            <tbody>
              {h.lot_details.map((l) => (
                <tr key={l.row}>
                  <td className="muted">{l.row}</td>
                  <td>{date(l.date)}</td>
                  <td className="num">
                    {qty(l.held_qty)}
                    {l.held_qty !== l.qty && <div className="muted small">bought {qty(l.qty)}</div>}
                  </td>
                  <td className="num">{money(l.rate, true)}</td>
                  <td className="num">{money(l.charges, true)}</td>
                  <td className="num">{money(l.cost, true)}</td>
                  <td className="num">{money(l.value, true)}</td>
                  <td className={`num ${tone(l.gain)}`}>
                    {signedMoney(l.gain)} <span className="small">({pct(l.gain_pct, 1, true)})</span>
                  </td>
                  <td>
                    {l.term === "LT" ? (
                      <Badge tone="pos">Long term</Badge>
                    ) : l.term === "ST" ? (
                      <span className="row" style={{ gap: 6 }}>
                        <Badge tone="warn">Short term</Badge>
                        <span className="muted small">LT on {date(l.lt_date)}</span>
                      </span>
                    ) : (
                      <Badge>no date</Badge>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </td>
      </tr>
    </>
  );
}
