import { motion } from "framer-motion";
import { useState } from "react";

import Logo from "../components/Logo";
import SystemStatus from "../components/SystemStatus";

export default function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [note, setNote] = useState<string | null>(null);

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    // Phase 0: auth is not wired yet — that lands in Phase 1.
    setNote("Авторизация подключается в фазе 1. Каркас жив.");
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
          <p className="mt-1 text-xs uppercase tracking-[0.2em] text-slate-500">
            Security testing console
          </p>
        </div>

        <form onSubmit={onSubmit} className="mt-8 space-y-3">
          <input
            type="email"
            className="field"
            placeholder="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="username"
          />
          <input
            type="password"
            className="field"
            placeholder="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
          />
          <button type="submit" className="btn-primary">
            <span className="relative z-10">Войти</span>
          </button>
        </form>

        {note && (
          <motion.p
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            className="mt-4 text-center text-xs text-accent-soft"
          >
            {note}
          </motion.p>
        )}

        <div className="mt-8 border-t border-white/5 pt-5">
          <SystemStatus />
        </div>
      </motion.div>
    </div>
  );
}
