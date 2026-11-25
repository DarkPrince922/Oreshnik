import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useState } from "react";

import { setConfirmListener, type ConfirmRequest } from "../lib/confirm";

/** Renders the in-app confirmation dialog (see lib/confirm.ts). Mounted once at the app root. */
export default function ConfirmHost() {
  const [req, setReq] = useState<ConfirmRequest | null>(null);

  useEffect(() => {
    setConfirmListener(setReq);
    return () => setConfirmListener(null);
  }, []);

  useEffect(() => {
    if (!req) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") answer(false);
      if (e.key === "Enter") answer(true);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [req]);

  const answer = (ok: boolean) => {
    req?.resolve(ok);
    setReq(null);
  };

  return (
    <AnimatePresence>
      {req && (
        <motion.div
          className="fixed inset-0 z-[70] flex items-center justify-center bg-black/70 px-4 backdrop-blur-sm"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onMouseDown={(e) => e.target === e.currentTarget && answer(false)}
        >
          <motion.div
            role="alertdialog"
            aria-modal="true"
            initial={{ opacity: 0, y: 16, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 10, scale: 0.98 }}
            transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
            className="glass w-full max-w-md rounded-2xl p-6 shadow-[0_30px_90px_-30px_rgba(0,0,0,0.8)]"
          >
            <p className="text-[15px] leading-relaxed text-slate-100 [overflow-wrap:anywhere]">{req.message}</p>
            <div className="mt-6 flex justify-end gap-2">
              <button
                type="button"
                autoFocus
                onClick={() => answer(false)}
                className="rounded-xl border border-white/10 bg-white/5 px-4 py-2 text-sm text-slate-200 transition hover:bg-white/10"
              >
                Отмена
              </button>
              <button
                type="button"
                onClick={() => answer(true)}
                className="rounded-xl bg-signal-bad/90 px-4 py-2 text-sm font-semibold text-white transition hover:bg-signal-bad"
              >
                Подтвердить
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
