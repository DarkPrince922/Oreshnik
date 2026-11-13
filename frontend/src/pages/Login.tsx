import { motion } from "framer-motion";
import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import Logo from "../components/Logo";
import SystemStatus from "../components/SystemStatus";
import { Button, ErrorNote } from "../components/ui";
import { errorMessage } from "../lib/api";
import { useAuth } from "../lib/auth";

export default function Login() {
  const { user, login } = useAuth();
  const navigate = useNavigate();
  const from = (useLocation().state as { from?: string } | null)?.from ?? "/";

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (user) return <Navigate to={from} replace />;

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await login(email, password);
      navigate(from, { replace: true });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center px-4 py-10">
      <motion.div
        initial={{ opacity: 0, y: 24, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
        className="glass w-full max-w-sm rounded-3xl p-8"
      >
        <div className="flex flex-col items-center text-center">
          <Logo size={76} />
          <motion.h1
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.25 }}
            className="mt-6 text-2xl font-semibold tracking-tight text-white"
          >
            Oreshnik
          </motion.h1>
          <p className="mt-1 text-xs uppercase tracking-[0.2em] text-slate-500">Security testing console</p>
        </div>

        <form onSubmit={onSubmit} className="mt-8 space-y-3">
          <input
            type="text"
            inputMode="email"
            className="field"
            placeholder="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="username"
            autoFocus
            required
          />
          <input
            type="password"
            className="field"
            placeholder="пароль"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
          <ErrorNote message={error} />
          <Button type="submit" variant="primary" loading={busy} className="w-full py-3">
            Войти
          </Button>
        </form>

        <div className="mt-8 border-t border-white/5 pt-5">
          <SystemStatus />
        </div>
      </motion.div>
    </div>
  );
}
