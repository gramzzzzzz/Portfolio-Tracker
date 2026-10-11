import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Alert } from "../components/ui";

export default function Login() {
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const nav = useNavigate();

  useEffect(() => {
    api.get<{ configured: boolean; authenticated: boolean }>("/api/auth/status").then((s) => {
      if (s.authenticated) nav("/", { replace: true });
      setConfigured(s.configured);
    });
  }, [nav]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setErr("");
    if (!configured && pw !== pw2) return setErr("Passwords do not match");
    setBusy(true);
    try {
      await api.send("POST", configured ? "/api/auth/login" : "/api/auth/setup", { password: pw });
      nav("/", { replace: true });
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (configured === null) return null;
  return (
    <div className="login">
      <form className="card" onSubmit={submit}>
        <div className="card-body stack">
          <div>
            <h1>{configured ? "Welcome back" : "Set up Portfolio Tracker"}</h1>
            <p className="muted" style={{ margin: "6px 0 0" }}>
              {configured ? "Sign in to manage your portfolio." : "Choose a password that protects this app on your network."}
            </p>
          </div>
          {err && <Alert tone="neg">{err}</Alert>}
          <label className="field">
            <span>Password</span>
            <input className="input" type="password" autoComplete={configured ? "current-password" : "new-password"} value={pw} onChange={(e) => setPw(e.target.value)} minLength={configured ? 1 : 8} required autoFocus />
          </label>
          {!configured && (
            <label className="field">
              <span>Confirm password</span>
              <input className="input" type="password" autoComplete="new-password" value={pw2} onChange={(e) => setPw2(e.target.value)} minLength={8} required />
              <span className="hint">At least 8 characters.</span>
            </label>
          )}
          <button className="btn primary" disabled={busy}>
            {busy ? "Please wait…" : configured ? "Sign in" : "Create password"}
          </button>
        </div>
      </form>
    </div>
  );
}
