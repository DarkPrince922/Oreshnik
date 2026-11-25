import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { setConfirmListener, type ConfirmRequest } from "../lib/confirm";

/** Renders the in-app confirmation dialog (see lib/confirm.ts). Mounted once at the app root. */
export default function ConfirmHost() {
  const cancelButton = useRef<HTMLButtonElement>(null);
  const trigger = useRef<HTMLElement | null>(null);
  const [req, setReq] = useState<ConfirmRequest | null>(null);

  useEffect(() => {
    setConfirmListener(setReq);
    return () => setConfirmListener(null);
  }, []);

  useLayoutEffect(() => {
    if (req) {
      if (!trigger.current && document.activeElement instanceof HTMLElement) {
        trigger.current = document.activeElement;
      }
      cancelButton.current?.focus();
    } else {
      trigger.current?.focus();
      trigger.current = null;
    }
  }, [req]);

  return (
    <AnimatePresence>
      {req && (
        <motion.div
          className="fixed inset-0 z-[70] flex items-center justify-center bg-black/70 px-4 backdrop-blur-sm"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onMouseDown={(e) => e.target === e.currentTarget && req.resolve(false)}
        >
          <motion.div
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="confirm-message"
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                req.resolve(false);
              }
              if (event.key === "Tab") {
                event.preventDefault();
                const buttons = [...event.currentTarget.querySelectorAll("button")];
                buttons.find((button) => button !== document.activeElement)?.focus();
              }
            }}
            initial={{ opacity: 0, y: 16, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 10, scale: 0.98 }}
            transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
            className="glass w-full max-w-md rounded-2xl p-6 shadow-[0_30px_90px_-30px_rgba(0,0,0,0.8)]"
          >
            <p id="confirm-message" className="text-[15px] leading-relaxed text-slate-100 [overflow-wrap:anywhere]">{req.message}</p>
            <div className="mt-6 flex justify-end gap-2">
              <button
                type="button"
                ref={cancelButton}
                onClick={() => req.resolve(false)}
                className="rounded-xl border border-white/10 bg-white/5 px-4 py-2 text-sm text-slate-200 transition hover:bg-white/10"
              >
                Отмена
              </button>
              <button
                type="button"
                onClick={() => req.resolve(true)}
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
