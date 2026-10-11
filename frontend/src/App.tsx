import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { NavLink, Navigate, Outlet, Route, Routes, useNavigate } from "react-router-dom";
import { api, download, type Version } from "./api";
import { Icon, Spinner, useToast, type IconName } from "./components/ui";
import { dateTime } from "./format";
import Dashboard from "./pages/Dashboard";
import Holdings from "./pages/Holdings";
import ImportPage from "./pages/Import";
import Insights from "./pages/Insights";
import History from "./pages/History";
import SettingsPage from "./pages/Settings";
import Login from "./pages/Login";
import Onboarding from "./pages/Onboarding";

// --------------------------------------------------------------- app context
interface AppState {
  head: Version | null | undefined; // undefined = loading, null = no sheet yet
  live: boolean;
  setLive: (v: boolean) => void;
  liveAllowed: boolean;
  refreshHead: () => Promise<void>;
  epoch: number; // bumps whenever the sheet changes -> pages refetch
}
const Ctx = createContext<AppState>(null as unknown as AppState);
export const useApp = () => useContext(Ctx);

function AppProvider({ children }: { children: ReactNode }) {
  const [head, setHead] = useState<Version | null | undefined>(undefined);
  const [epoch, setEpoch] = useState(0);
  const [liveAllowed, setLiveAllowed] = useState(true);
  const [live, setLiveState] = useState(() => localStorage.getItem("pt.live") !== "0");
  const refreshHead = useCallback(async () => {
    const vs = await api.get<Version[]>("/api/versions");
    setHead(vs.find((v) => v.is_head) || null);
    setEpoch((e) => e + 1);
  }, []);
  useEffect(() => {
    refreshHead().catch(() => setHead(null));
    api.get<{ live_prices: boolean }>("/api/settings").then((s) => setLiveAllowed(s.live_prices)).catch(() => {});
  }, [refreshHead]);
  const setLive = (v: boolean) => {
    localStorage.setItem("pt.live", v ? "1" : "0");
    setLiveState(v);
  };
  return <Ctx.Provider value={{ head, live: live && liveAllowed, setLive, liveAllowed, refreshHead, epoch }}>{children}</Ctx.Provider>;
}

// --------------------------------------------------------------- shell
const NAV: { to: string; label: string; icon: IconName }[] = [
  { to: "/", label: "Dashboard", icon: "dashboard" },
  { to: "/holdings", label: "Holdings", icon: "holdings" },
  { to: "/import", label: "Import notes", icon: "upload" },
  { to: "/insights", label: "Insights", icon: "insights" },
  { to: "/history", label: "Sheet history", icon: "history" },
  { to: "/settings", label: "Settings", icon: "settings" },
];

function Shell() {
  const { head, live, setLive, liveAllowed } = useApp();
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
            {liveAllowed && (
              <div className="seg" role="group" aria-label="Price source">
                <button aria-pressed={!live} onClick={() => setLive(false)} title="Use the market rates typed in the sheet">
                  Sheet prices
                </button>
                <button aria-pressed={live} onClick={() => setLive(true)} title="Use live market quotes (display only, never written to the sheet)">
                  Live prices
                </button>
              </div>
            )}
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
        <Route path="insights" element={<Insights />} />
        <Route path="history" element={<History />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
