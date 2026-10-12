import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { NavLink, Navigate, Outlet, Route, Routes, useNavigate } from "react-router-dom";
import { api, download, type PriceSync, type Version } from "./api";
import { Icon, Spinner, useToast, type IconName } from "./components/ui";
import { dateTime, timeAgo } from "./format";
import Dashboard from "./pages/Dashboard";
import Holdings from "./pages/Holdings";
import ImportPage from "./pages/Import";
import Insights from "./pages/Insights";
import TaxPlanner from "./pages/TaxPlanner";
import History from "./pages/History";
import SettingsPage from "./pages/Settings";
import Login from "./pages/Login";
import Onboarding from "./pages/Onboarding";

// --------------------------------------------------------------- app context
interface AppState {
  head: Version | null | undefined; // undefined = loading, null = no sheet yet
  sync: PriceSync | null;
  refreshHead: () => Promise<void>;
  syncNow: () => Promise<PriceSync>;
  epoch: number; // bumps whenever the sheet changes -> pages refetch
}
const Ctx = createContext<AppState>(null as unknown as AppState);
export const useApp = () => useContext(Ctx);

function AppProvider({ children }: { children: ReactNode }) {
  const [head, setHead] = useState<Version | null | undefined>(undefined);
  const [sync, setSync] = useState<PriceSync | null>(null);
  const [epoch, setEpoch] = useState(0);
  const refreshHead = useCallback(async () => {
    const vs = await api.get<Version[]>("/api/versions");
    setHead(vs.find((v) => v.is_head) || null);
    setEpoch((e) => e + 1);
  }, []);
  const loadSync = useCallback(() => api.get<PriceSync>("/api/prices/sync").then(setSync).catch(() => {}), []);
  const syncNow = useCallback(async () => {
    const st = await api.send<PriceSync>("POST", "/api/prices/sync");
    setSync(st);
    await refreshHead();
    return st;
  }, [refreshHead]);
  useEffect(() => {
    refreshHead().catch(() => setHead(null));
    loadSync();
    const t = setInterval(loadSync, 60_000); // keep "written to sheet …" fresh
    return () => clearInterval(t);
  }, [refreshHead, loadSync]);
  return <Ctx.Provider value={{ head, sync, refreshHead, syncNow, epoch }}>{children}</Ctx.Provider>;
}

// --------------------------------------------------------------- shell
const NAV: { to: string; label: string; icon: IconName }[] = [
  { to: "/", label: "Dashboard", icon: "dashboard" },
  { to: "/holdings", label: "Holdings", icon: "holdings" },
  { to: "/import", label: "Import notes", icon: "upload" },
  { to: "/tax", label: "Tax planner", icon: "tax" },
  { to: "/insights", label: "Insights", icon: "insights" },
  { to: "/history", label: "Sheet history", icon: "history" },
  { to: "/settings", label: "Settings", icon: "settings" },
];

function Shell() {
  const { head, sync, syncNow } = useApp();
  const [syncing, setSyncing] = useState(false);
  const nav = useNavigate();
  const toast = useToast();
  if (head === undefined) return <Spinner />;
  if (head === null) return <Onboarding />;
  const logout = async () => {
    await api.send("POST", "/api/auth/logout");
    nav("/login");
  };
  return (
    <div className="shell">
      <aside className="sidebar" aria-label="Main navigation">
        <div className="brand">
          <span className="brand-mark">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth={3} strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <path d="M4 17l6-6 4 4 7-8" />
            </svg>
          </span>
          Portfolio Tracker
        </div>
        <nav className="nav">
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.to === "/"}>
              <Icon name={n.icon} />
              {n.label}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-foot">
          <button className="btn ghost sm" onClick={logout} style={{ justifyContent: "flex-start" }}>
            <Icon name="logout" /> Sign out
          </button>
        </div>
      </aside>
      <div className="main">
        <header className="topbar">
          <div className="meta">
            <span className="row" title={head.message}>
              <Icon name="sheet" />
              <strong style={{ color: "var(--text)" }}>Sheet v{head.id}</strong>
              <span>updated {dateTime(head.created_at)}</span>
            </span>
          </div>
          <div className="row">
            <PriceStatus
              sync={sync}
              busy={syncing}
              onSync={async () => {
                setSyncing(true);
                try {
                  const st = await syncNow();
                  toast(st.result === "written" ? `Market rates written to the sheet (${st.rows_updated} rows)` : st.result === "unchanged" ? "Sheet already has the latest prices" : st.detail || "Price sync finished");
                } catch (e) {
                  toast((e as Error).message, "neg");
                } finally {
                  setSyncing(false);
                }
              }}
            />
            <button
              className="btn sm"
              onClick={() => {
                download("/api/sheet/download");
                toast("Downloading the current sheet");
              }}
            >
              <Icon name="download" /> Download sheet
            </button>
            <button className="btn primary sm" onClick={() => nav("/import")}>
              <Icon name="upload" /> Import contract notes
            </button>
          </div>
        </header>
        <main className="content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

function PriceStatus({ sync, busy, onSync }: { sync: PriceSync | null; busy: boolean; onSync: () => void }) {
  if (!sync) return null;
  if (!sync.enabled) return <span className="badge warn" title="Live prices are disabled on the server (PT_LIVE_PRICES=false)">Offline: sheet rates</span>;
  const err = sync.result === "error" || sync.result === "no_quotes";
  const title = `Live prices on every page. Market rates are written into the sheet every ${sync.interval_minutes} min` +
    (sync.last_write ? `; last written ${dateTime(sync.last_write)}` : "") + (sync.detail ? ` — ${sync.detail}` : "");
  return (
    <span className="row small" style={{ gap: 6 }} title={title}>
      <span className={`dot ${err ? "neg" : "pos"}`} aria-hidden />
      <span>Live prices</span>
      <span className="muted">· sheet updated {sync.last_write ? timeAgo(sync.last_write) : "—"}</span>
      <button className="btn ghost sm" onClick={onSync} disabled={busy} aria-label="Write latest prices to the sheet now">
        <Icon name="refresh" />
      </button>
    </span>
  );
}

function Guard({ children }: { children: ReactNode }) {
  const [state, setState] = useState<"loading" | "ok" | "login">("loading");
  const nav = useNavigate();
  useEffect(() => {
    api
      .get<{ authenticated: boolean }>("/api/auth/status")
      .then((s) => setState(s.authenticated ? "ok" : "login"))
      .catch(() => setState("login"));
    const h = () => nav("/login");
    window.addEventListener("pt:unauthorized", h);
    return () => window.removeEventListener("pt:unauthorized", h);
  }, [nav]);
  if (state === "loading") return <Spinner />;
  if (state === "login") return <Navigate to="/login" replace />;
  return <AppProvider>{children}</AppProvider>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        element={
          <Guard>
            <Shell />
          </Guard>
        }
      >
        <Route index element={<Dashboard />} />
        <Route path="holdings" element={<Holdings />} />
        <Route path="import" element={<ImportPage />} />
        <Route path="import/:batchId" element={<ImportPage />} />
        <Route path="tax" element={<TaxPlanner />} />
        <Route path="insights" element={<Insights />} />
        <Route path="history" element={<History />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
