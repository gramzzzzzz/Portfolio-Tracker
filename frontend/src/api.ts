// Thin typed client for the backend API.

export class ApiError extends Error {
  status: number;
  code?: string;
  constructor(status: number, message: string, code?: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

const HDR = { "X-Requested-With": "portfolio-tracker" };

async function handle<T>(r: Response): Promise<T> {
  if (r.ok) {
    const ct = r.headers.get("content-type") || "";
    return (ct.includes("json") ? r.json() : r.blob()) as Promise<T>;
  }
  let msg = r.statusText;
  let code: string | undefined;
  try {
    const j = await r.json();
    code = j.code;
    msg = typeof j.detail === "string" ? j.detail : Array.isArray(j.detail) ? j.detail.map((d: any) => d.msg).join("; ") : msg;
  } catch {
    /* not json */
  }
  if (r.status === 401 && !location.pathname.startsWith("/login")) {
    window.dispatchEvent(new Event("pt:unauthorized"));
  }
  throw new ApiError(r.status, msg, code);
}

export const api = {
  get: <T,>(url: string) => fetch(url, { credentials: "same-origin" }).then(handle<T>),
  send: <T,>(method: string, url: string, body?: unknown) =>
    fetch(url, {
      method,
      credentials: "same-origin",
      headers: { ...HDR, ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    }).then(handle<T>),
  form: <T,>(url: string, fd: FormData) =>
    fetch(url, { method: "POST", credentials: "same-origin", headers: HDR, body: fd }).then(handle<T>),
};

export function download(url: string) {
  const a = document.createElement("a");
  a.href = url;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// ---------------------------------------------------------------- types
export interface Version {
  id: number;
  created_at: number;
  sha256: string;
  size: number;
  message: string;
  source: "initial" | "upload" | "import" | "restore";
  parent_id: number | null;
  batch_id: string | null;
  filename: string | null;
  stats: { lots?: number; holdings?: number; invested?: number; issues?: number; lots_delta?: number };
  is_head?: boolean;
}

export interface Issue {
  severity: "info" | "warning" | "error";
  code: string;
  message: string;
  rows: number[];
}

export interface LotDetail {
  row: number;
  name: string;
  date: string | null;
  qty: number;
  held_qty: number;
  rate: number;
  charges: number;
  cost: number;
  unit_cost: number | null;
  price: number | null;
  price_source: string;
  value: number;
  gain: number;
  gain_pct: number | null;
  days: number | null;
  term: "LT" | "ST" | "?";
  lt_date: string | null;
  tax_class: string;
  sector: string | null;
  block: string | null;
  code: string | null;
}

export interface Holding {
  name: string;
  sector: string | null;
  block: string | null;
  code: string | null;
  symbol: string | null;
  qty: number;
  avg_cost: number | null;
  invested: number;
  price: number | null;
  price_source: string;
  value: number;
  gain: number;
  gain_pct: number | null;
  xirr: number | null;
  weight: number | null;
  lots: number;
  first_date: string | null;
  last_date: string | null;
  lt_qty: number;
  st_qty: number;
  day_change_pct: number | null;
  high52: number | null;
  low52: number | null;
  from_high_pct: number | null;
  rows: number[];
  lot_details: LotDetail[];
  tax_if_sold?: number;
  exit_costs?: number;
  after_tax_value?: number;
  after_tax_gain?: number;
}

export interface PriceSync {
  enabled: boolean;
  interval_minutes: number;
  last_run?: number;
  last_write?: number;
  result?: "written" | "unchanged" | "error" | "no_quotes" | "no_sheet" | "disabled";
  detail?: string;
  rows_updated?: number;
  missing?: string[];
  valuation_date?: string;
  valuation_cell?: string;
}

export interface AllocRow {
  name: string;
  value: number;
  invested: number;
  weight: number;
  holdings?: number;
}

export interface Overview {
  meta: {
    valuation_date: string | null;
    price_source: "live" | "last_sync";
    live_coverage: number | null;
    price_sync: PriceSync;
    as_of: string;
    sheet_name: string;
    version: Version;
  };
  kpis: {
    invested: number;
    value: number;
    gain: number;
    gain_pct: number | null;
    xirr: number | null;
    day_change: number | null;
    day_change_pct: number | null;
    holdings: number;
    lots: number;
    charges: number;
    charges_pct: number | null;
    sheet_grand_total: number | null;
    tax_if_sold?: number;
    exit_costs?: number;
    after_tax_value?: number;
    after_tax_gain?: number;
    after_tax_gain_pct?: number | null;
  };
  holdings: Holding[];
  allocation: { sector: AllocRow[]; block: AllocRow[]; asset_class: AllocRow[] };
  concentration: {
    top1: number | null;
    top5: number | null;
    hhi: number | null;
    effective_n: number | null;
    alerts: { type: string; name: string; weight: number; limit: number }[];
  };
  timeline: { month: string; invested: number; cumulative: number; buys: number }[];
  issues: Issue[];
}

export interface TradeT {
  isin: string;
  security_name: string;
  side: "BUY" | "SELL";
  qty: number;
  rate: number;
  gross_value: number;
  brokerage: number;
  gst: number;
  stt: number;
  other_levies: number;
  net_value: number;
  trade_date: string;
  charges: number;
  fills: { order_no: string; trade_no: string; trade_time: string; exchange: string; qty: number; price: number }[];
}

export interface Proposal {
  include: boolean;
  mode: "existing" | "new";
  name: string;
  sector: string | null;
  code: string | null;
  face_value: number | null;
  market_rate: number | null;
  symbol: string | null;
  sector_hint: string | null;
}

export interface ImportItem {
  id: string;
  file_id: string;
  trade: TradeT;
  status: "new" | "duplicate" | "possible_duplicate" | "sell";
  reason: string;
  matched_rows: number[];
  resolution: { sheet_name: string | null; confidence: number; reason: string; candidates: { name: string; score: number }[] };
  proposal: Proposal;
}

export interface ImportFile {
  id: string;
  filename: string;
  size: number;
  status: "pending" | "parsed" | "error" | "password_error" | "duplicate_file";
  error?: string;
  note?: {
    broker: string;
    note_no: string;
    trade_date: string;
    settlement_date: string | null;
    charges: Record<string, number>;
    charges_total: number;
    net_payable: number;
    warnings: string[];
  };
}

export interface PlanResult {
  rows: { kind: string; row: number; description: string; ref: string | null; values: Record<string, unknown> }[];
  checks: { name: string; passed: boolean; detail: string }[];
  warnings: string[];
  version?: Version;
}

export interface Batch {
  id: string;
  status: "staged" | "committed" | "discarded";
  created_at: number;
  base_version_id: number | null;
  result_version_id: number | null;
  stale: boolean;
  files: ImportFile[];
  items: ImportItem[];
  summary: { files: number; files_ok: number; files_failed: number; trades: number; new: number; duplicates: number; possible: number; sells: number };
  result: PlanResult | null;
  sheet: { securities: string[]; sectors: string[] };
}

export interface Settings {
  benchmark_symbol: string;
  price_sync_minutes: number;
  max_holding_pct: number;
  max_sector_pct: number;
  stcg_rate_pct: number;
  ltcg_rate_pct: number;
  ltcg_exemption: number;
  slab_rate_pct: number;
  surcharge_pct: number;
  cess_pct: number;
  realized_fy: string;
  realized_stcg_fy: number;
  realized_ltcg_fy: number;
  realized_st_other_fy: number;
  realized_lt_other_fy: number;
  sell_expense_pct: number | null;
  dp_charge_per_sale: number;
  upcoming_lt_window_days: number;
  tax_class_by_sector: Record<string, string>;
  sector_block_overrides: Record<string, string>;
  has_pdf_password: boolean;
}
