// Number / date formatting helpers (Indian conventions).

const inr0 = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
const inr2 = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", minimumFractionDigits: 2, maximumFractionDigits: 2 });
const num = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 4 });

export const money = (v: number | null | undefined, decimals = false) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : (decimals ? inr2 : inr0).format(v);

export const signedMoney = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : (v > 0 ? "+" : v < 0 ? "−" : "") + inr0.format(Math.abs(v));

export const compactMoney = (v: number | null | undefined) => {
  if (v === null || v === undefined) return "—";
  const a = Math.abs(v);
  const s = v < 0 ? "−" : "";
  if (a >= 1e7) return `${s}₹${(a / 1e7).toFixed(2)} Cr`;
  if (a >= 1e5) return `${s}₹${(a / 1e5).toFixed(2)} L`;
  if (a >= 1e3) return `${s}₹${(a / 1e3).toFixed(1)}k`;
  return `${s}₹${a.toFixed(0)}`;
};

export const pct = (v: number | null | undefined, digits = 1, signed = false) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : `${signed && v > 0 ? "+" : ""}${v.toFixed(digits)}%`;

export const qty = (v: number | null | undefined) => (v === null || v === undefined ? "—" : num.format(v));

export const date = (iso: string | null | undefined) => {
  if (!iso) return "—";
  const d = new Date(iso.length === 10 ? iso + "T00:00:00" : iso);
  return d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
};

export const dateTime = (epochSeconds: number) =>
  new Date(epochSeconds * 1000).toLocaleString("en-IN", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });

export const tone = (v: number | null | undefined) => (v === null || v === undefined || v === 0 ? "" : v > 0 ? "pos" : "neg");

export const bytes = (n: number) => (n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} KB`);

export const timeAgo = (epochSeconds: number) => {
  const m = Math.round((Date.now() / 1000 - epochSeconds) / 60);
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} h ago`;
  return dateTime(epochSeconds);
};
