import { useEffect, useState } from "react";
import { api, type Settings } from "../api";
import { useApp } from "../App";
import { timeAgo } from "../format";
import { EmailSettings } from "../components/Email";
import { Alert, Badge, Card, Icon, Spinner, useAsync, useToast } from "../components/ui";
import { dateTime } from "../format";

export default function SettingsPage() {
  const toast = useToast();
  const { epoch } = useApp();
  const { data, error, loading, reload } = useAsync(() => api.get<Settings>("/api/settings"), []);
  const structure = useAsync(() => api.get<any>("/api/sheet/structure"), [epoch]);
  const audit = useAsync(() => api.get<any[]>("/api/audit"), [epoch]);
  const [s, setS] = useState<Settings | null>(null);
  const [pdfPw, setPdfPw] = useState("");
  const [loginPw, setLoginPw] = useState("");
  useEffect(() => {
    if (data) setS(data);
  }, [data]);
  useEffect(() => {
    if (s && location.hash) setTimeout(() => document.querySelector(location.hash)?.scrollIntoView({ block: "start" }), 50);
  }, [!!s]); // eslint-disable-line react-hooks/exhaustive-deps
  if (loading && !s) return <Spinner />;
  if (error) return <Alert tone="neg">{error.message}</Alert>;
  if (!s) return null;

  const save = async (patch: Partial<Settings>) => {
    try {
      const n = await api.send<Settings>("PUT", "/api/settings", patch);
      // take back only what was saved, so unsaved edits in other sections survive
      setS((cur) => {
        if (!cur) return n;
        const next = { ...cur } as Record<string, unknown>;
        for (const k of Object.keys(patch)) next[k] = (n as unknown as Record<string, unknown>)[k];
        next.has_pdf_password = n.has_pdf_password;
        return next as unknown as Settings;
      });
      toast("Settings saved");
    } catch (e) {
      toast((e as Error).message, "neg");
    }
  };
  const num = (k: keyof Settings, label: string, hint?: string, step = "0.5") => (
    <label className="field">
      <span>{label}</span>
      <input className="input" type="number" min={0} step={step} value={String(s[k])} onChange={(e) => setS({ ...s, [k]: e.target.value } as Settings)} />
      {hint && <span className="hint">{hint}</span>}
    </label>
  );
  const sectors: string[] = structure.data?.sectors.map((x: any) => x.name) || [];
  const blocks: string[] = (structure.data?.sections || []).flatMap((sec: any) => sec.blocks.map((b: any) => b.header).filter(Boolean));

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Settings</h1>
          <p>Passwords, tax assumptions, alert thresholds and how sectors map to the sheet.</p>
        </div>
      </div>

      <div className="grid g-2">
        <Card title={<span className="row"><Icon name="lock" /> Contract note password</span>} subtitle="Used to open password-protected PDFs. Stored encrypted on the server.">
          <div className="stack">
            <div>{s.has_pdf_password ? <Badge tone="pos">A password is saved</Badge> : <Badge>No password saved</Badge>}</div>
            <div className="row">
              <input className="input" style={{ maxWidth: 260 }} type="password" autoComplete="off" placeholder="New PDF password" value={pdfPw} onChange={(e) => setPdfPw(e.target.value)} aria-label="New PDF password" />
              <button
                className="btn primary"
                disabled={!pdfPw}
                onClick={async () => {
                  await api.send("PUT", "/api/settings/pdf-password", { password: pdfPw });
                  setPdfPw("");
                  reload();
                  toast("PDF password saved");
                }}
              >
                Save
              </button>
              {s.has_pdf_password && (
                <button
                  className="btn danger"
                  onClick={async () => {
                    await api.send("DELETE", "/api/settings/pdf-password");
                    reload();
                    toast("PDF password removed");
                  }}
                >
                  Forget
                </button>
              )}
            </div>
          </div>
        </Card>
        <Card title="App sign-in password">
          <div className="row">
            <input className="input" style={{ maxWidth: 260 }} type="password" autoComplete="new-password" minLength={8} placeholder="New password (min 8 chars)" value={loginPw} onChange={(e) => setLoginPw(e.target.value)} aria-label="New sign-in password" />
            <button
              className="btn"
              disabled={loginPw.length < 8}
              onClick={async () => {
                await api.send("PUT", "/api/settings/login-password", { password: loginPw });
                setLoginPw("");
                toast("Sign-in password changed");
              }}
            >
              Change
            </button>
          </div>
        </Card>
      </div>

      <EmailSettings />

      <PriceSyncCard s={s} setS={setS} save={save} />

      <Card title="Tax assumptions" subtitle="Used by the Tax planner, the after-tax figures on the dashboard and holdings, and harvesting suggestions">
        <div className="grid g-kpi">
          {num("stcg_rate_pct", "STCG rate on equity (%)")}
          {num("ltcg_rate_pct", "LTCG rate (%)")}
          {num("ltcg_exemption", "Equity LTCG exemption per FY (₹)", undefined, "1000")}
          {num("slab_rate_pct", "Your income-tax slab (%)", "Short-term gold ETF / debt fund gains are taxed at this rate")}
          {num("surcharge_pct", "Surcharge (%)", "0 below ₹50 L income; max 15% on these gains", "5")}
          {num("cess_pct", "Health & education cess (%)", undefined, "1")}
          {num("equity_lt_months", "Shares / equity ETFs: long term after (months)", "12 under current rules", "1")}
          {num("upcoming_lt_window_days", "Show lots turning LT within (days)", undefined, "5")}
        </div>
        <h3 style={{ margin: "18px 0 8px" }}>Securities transaction tax (STT)</h3>
        <div className="grid g-kpi">
          {num("stt_buy_share_pct", "STT on buying shares (%)", "Only used for rows without a contract note", "0.001")}
          {num("stt_sell_share_pct", "STT on selling shares (%)", undefined, "0.001")}
          {num("stt_sell_fund_pct", "STT on selling equity ETFs / funds (%)", undefined, "0.001")}
        </div>
        <h3 style={{ margin: "18px 0 8px" }}>Selling costs</h3>
        <div className="grid g-kpi">
          <label className="field">
            <span>Brokerage + exchange + GST (% of sale)</span>
            <input className="input" type="number" min={0} step="0.001" placeholder="Auto: from your contract notes" value={s.sell_expense_pct ?? ""} onChange={(e) => setS({ ...s, sell_expense_pct: e.target.value === "" ? null : (e.target.value as unknown as number) })} />
            <span className="hint">Leave empty to use what your imported contract notes show. STT is added separately.</span>
          </label>
          {num("dp_charge_per_sale", "DP charge per holding sold (₹)", "Depository charge your broker levies per scrip per sell day", "1")}
        </div>
        <div style={{ marginTop: 14 }}>
          <button
            className="btn primary"
            onClick={() =>
              save({
                stcg_rate_pct: s.stcg_rate_pct,
                ltcg_rate_pct: s.ltcg_rate_pct,
                ltcg_exemption: s.ltcg_exemption,
                slab_rate_pct: s.slab_rate_pct,
                surcharge_pct: s.surcharge_pct,
                cess_pct: s.cess_pct,
                upcoming_lt_window_days: s.upcoming_lt_window_days,
                equity_lt_months: s.equity_lt_months,
                stt_buy_share_pct: s.stt_buy_share_pct,
                stt_sell_share_pct: s.stt_sell_share_pct,
                stt_sell_fund_pct: s.stt_sell_fund_pct,
                sell_expense_pct: s.sell_expense_pct,
                dp_charge_per_sale: s.dp_charge_per_sale,
              })
            }
          >
            Save tax settings
          </button>
          <span className="muted small" style={{ marginLeft: 10 }}>Gains you already booked this year are entered on the Tax planner page.</span>
        </div>
      </Card>

      <GoldSettings s={s} setS={setS} save={save} />

      <Card title="Diversification alerts">
        <div className="grid g-kpi">
          {num("max_holding_pct", "Max weight of one holding (%)", undefined, "1")}
          {num("max_sector_pct", "Max weight of one sector (%)", undefined, "1")}
        </div>
        <div style={{ marginTop: 14 }}>
          <button className="btn primary" onClick={() => save({ max_holding_pct: s.max_holding_pct, max_sector_pct: s.max_sector_pct })}>
            Save limits
          </button>
        </div>
      </Card>

      <Card title="Sectors and the sheet" subtitle="Where new holdings of each sector are placed, and how they are taxed" pad={false}>
        {structure.data ? (
          <table className="t">
            <thead>
              <tr>
                <th>Sector (Summary / sector table)</th>
                <th>Placed under heading</th>
                <th>Tax treatment</th>
              </tr>
            </thead>
            <tbody>
              {sectors.map((sec) => (
                <tr key={sec}>
                  <td style={{ fontWeight: 600 }}>{sec}</td>
                  <td>
                    <select
                      className="input sm"
                      style={{ maxWidth: 240 }}
                      value={s.sector_block_overrides[sec] || ""}
                      onChange={(e) => save({ sector_block_overrides: { ...s.sector_block_overrides, [sec]: e.target.value } })}
                      aria-label={`Heading for ${sec}`}
                    >
                      <option value="">Automatic ({structure.data.sector_to_block[sec] || "new heading"})</option>
                      {blocks.map((b) => (
                        <option key={b}>{b}</option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <select
                      className="input sm"
                      style={{ maxWidth: 220 }}
                      value={s.tax_class_by_sector[sec] || ""}
                      onChange={(e) => save({ tax_class_by_sector: { ...s.tax_class_by_sector, [sec]: e.target.value } })}
                      aria-label={`Tax treatment for ${sec}`}
                    >
                      <option value="">Automatic</option>
                      <option value="equity">Listed equity (STT paid)</option>
                      <option value="non_equity">Gold / other ETF</option>
                      <option value="debt">Debt (slab rate)</option>
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Spinner />
        )}
      </Card>

      <Card title="Activity log" pad={false}>
        <div className="table-wrap" style={{ maxHeight: 320 }}>
          <table className="t">
            <tbody>
              {(audit.data || []).slice(0, 50).map((a) => (
                <tr key={a.id}>
                  <td className="nowrap muted small">{dateTime(a.ts)}</td>
                  <td>
                    <Badge>{a.action}</Badge>
                  </td>
                  <td className="small">{a.detail.message || a.detail.files?.join(", ") || (a.detail.rows ? `${a.detail.rows} rows → v${a.detail.version}` : "")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

function PriceSyncCard({ s, setS, save }: { s: Settings; setS: (s: Settings) => void; save: (p: Partial<Settings>) => Promise<void> }) {
  const { sync, syncNow } = useApp();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  return (
    <Card title="Market prices" subtitle="Pages always show live share and gold prices (refreshed when you open a page, at most every 5 minutes). On this schedule the app also writes them into the workbook: share market rates + valuation date in your main sheet, and current rate/value in the Gold sheet, so the file matches the app when opened on its own.">
      <div className="stack">
        {sync && !sync.enabled && <Alert tone="warn">Live prices are turned off on the server (PT_LIVE_PRICES=false); the app uses the market rates already in the sheet.</Alert>}
        {sync?.enabled && (
          <dl className="kv" style={{ maxWidth: 520 }}>
            <dt>Last written to the sheet</dt>
            <dd>{sync.last_write ? `${dateTime(sync.last_write)} (${timeAgo(sync.last_write)})` : "not yet"}</dd>
            <dt>Last check</dt>
            <dd>
              {sync.last_run ? dateTime(sync.last_run) : "—"} · {sync.result === "written" ? `${sync.rows_updated} rows updated` : sync.result === "unchanged" ? "no price changed" : sync.detail || sync.result}
            </dd>
            {sync.valuation_date && (
              <>
                <dt>Valuation date in sheet ({sync.valuation_cell})</dt>
                <dd>{sync.valuation_date}</dd>
              </>
            )}
            {sync.missing && sync.missing.length > 0 && (
              <>
                <dt>No live price for</dt>
                <dd>{sync.missing.join(", ")} — set a symbol on the Holdings page</dd>
              </>
            )}
          </dl>
        )}
        <div className="row" style={{ alignItems: "flex-end" }}>
          <label className="field" style={{ width: 200 }}>
            <span>Write share & gold prices every (minutes)</span>
            <input className="input" type="number" min={5} step={5} value={String(s.price_sync_minutes)} onChange={(e) => setS({ ...s, price_sync_minutes: e.target.value as unknown as number })} />
          </label>
          <button className="btn" onClick={() => save({ price_sync_minutes: s.price_sync_minutes })}>
            Save
          </button>
          <button
            className="btn"
            disabled={busy || !sync?.enabled}
            onClick={async () => {
              setBusy(true);
              try {
                const st = await syncNow();
                toast(st.result === "written" ? `${st.rows_updated} market rates written to the sheet` : st.detail || "Sheet already up to date");
              } finally {
                setBusy(false);
              }
            }}
          >
            <Icon name="refresh" /> Write prices now
          </button>
        </div>
        <div className="row" style={{ alignItems: "flex-end" }}>
          <label className="field" style={{ width: 240 }}>
            <span>Benchmark index symbol</span>
            <input className="input" value={s.benchmark_symbol} onChange={(e) => setS({ ...s, benchmark_symbol: e.target.value })} />
            <span className="hint">^NSEI = Nifty 50, ^CRSLDX = Nifty 500, ^BSESN = Sensex</span>
          </label>
          <button className="btn" onClick={() => save({ benchmark_symbol: s.benchmark_symbol })}>
            Save
          </button>
        </div>
      </div>
    </Card>
  );
}

function GoldSettings({ s, setS, save }: { s: Settings; setS: (s: Settings) => void; save: (p: Partial<Settings>) => Promise<void> }) {
  const n = (k: keyof Settings, label: string, hint?: string, step = "0.5") => (
    <label className="field">
      <span>{label}</span>
      <input className="input" type="number" min={0} step={step} value={s[k] === null || s[k] === undefined ? "" : String(s[k])} onChange={(e) => setS({ ...s, [k]: e.target.value === "" ? null : e.target.value } as Settings)} />
      {hint && <span className="hint">{hint}</span>}
    </label>
  );
  const [purity, setPurity] = useState(Object.entries(s.gold_purity).map(([k, v]) => `${k}=${v}`).join(", "));
  const keys: (keyof Settings)[] = ["gold_lt_months", "gold_ltcg_rate_pct", "gold_gst_pct", "gold_making_gst_pct", "gold_buyback_deduction_coin_pct", "gold_buyback_deduction_bar_pct", "gold_buyback_deduction_jewellery_pct", "gold_domestic_premium_pct", "gold_price_override_24k"];
  return (
    <section id="gold">
      <Card title="Physical gold" subtitle="Price, buy-back deductions and tax rules used on the Physical gold page. Defaults reflect the rules as of Oct 2026 — change them whenever the rules or your jeweller's terms change.">
        <h3 style={{ margin: "0 0 8px" }}>Price</h3>
        <div className="grid g-kpi">
          {n("gold_domestic_premium_pct", "Indian premium over international price (%)", "Import duty is 15% since 13 May 2026 (10% BCD + 5% AIDC); adjust to match local rates", "0.5")}
          {n("gold_price_override_24k", "Use my own 24K rate (₹ per gram)", "Leave empty to use the live price", "1")}
          <label className="field">
            <span>Purity table (name=fineness)</span>
            <input className="input" value={purity} onChange={(e) => setPurity(e.target.value)} />
            <span className="hint">e.g. 24K=0.999, 22K=0.916, 18K=0.75</span>
          </label>
        </div>
        <h3 style={{ margin: "18px 0 8px" }}>Selling</h3>
        <div className="grid g-kpi">
          {n("gold_buyback_deduction_coin_pct", "Buy-back deduction: coins (%)")}
          {n("gold_buyback_deduction_bar_pct", "Buy-back deduction: bars (%)")}
          {n("gold_buyback_deduction_jewellery_pct", "Buy-back deduction: jewellery (%)", "Melting/assay loss jewellers deduct")}
        </div>
        <h3 style={{ margin: "18px 0 8px" }}>Tax</h3>
        <div className="grid g-kpi">
          {n("gold_lt_months", "Gold: long term after (months)", "24 for physical gold", "1")}
          {n("gold_ltcg_rate_pct", "Gold: long-term tax rate (%)", "12.5%, no indexation; short term uses your slab rate")}
          {n("gold_gst_pct", "GST on gold value when buying (%)")}
          {n("gold_making_gst_pct", "GST on making charges (%)")}
        </div>
        <div style={{ marginTop: 14 }}>
          <button
            className="btn primary"
            onClick={() => {
              const table: Record<string, number> = {};
              purity.split(",").forEach((p) => {
                const [k, v] = p.split("=").map((x) => x.trim());
                if (k && v) table[k.toUpperCase()] = Number(v);
              });
              const patch: Partial<Settings> = { gold_purity: table };
              keys.forEach((k) => ((patch as Record<string, unknown>)[k] = s[k]));
              save(patch);
            }}
          >
            Save gold settings
          </button>
        </div>
      </Card>
    </section>
  );
}
