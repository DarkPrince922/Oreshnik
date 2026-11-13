import clsx from "clsx";
import { useCallback, useLayoutEffect, useRef, useState } from "react";

import { classifyLine, type LogBuffer } from "../lib/logStream";
import type { StreamState } from "../lib/useScanStream";
import { SEVERITY_STYLE } from "./scan";
import type { Severity } from "../lib/api";

const WINDOW = 1000;

const STATE_LABEL: Record<StreamState, { text: string; dot: string }> = {
  connecting: { text: "подключение…", dot: "bg-signal-warn" },
  live: { text: "в эфире", dot: "bg-signal-ok" },
  ended: { text: "лог завершён", dot: "bg-slate-500" },
  error: { text: "нет соединения", dot: "bg-signal-bad" },
};

function Line({ number, text }: { number: number; text: string }) {
  const { kind, severity } = classifyLine(text);
  const tone =
    kind === "panel"
      ? "text-accent-soft"
      : kind === "error"
        ? "text-signal-bad"
        : kind === "command"
          ? "text-slate-500"
          : kind === "finding"
            ? clsx("font-semibold", SEVERITY_STYLE[(severity as Severity) in SEVERITY_STYLE ? (severity as Severity) : "unknown"].text)
            : "text-slate-300";
  return (
    <div data-line={number} className="grid grid-cols-[3.25rem_1fr] hover:bg-white/[0.03]">
      <span className="select-none pr-3 text-right text-slate-600">{number}</span>
      {/* Plain text node: log content comes from the scanned target and must never be interpreted as HTML. */}
      <span className={clsx("whitespace-pre-wrap [overflow-wrap:anywhere]", tone)}>{text || " "}</span>
    </div>
  );
}

export default function LogViewer({ buffer, state, active }: { buffer: LogBuffer; state: StreamState; active: boolean }) {
  const boxRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const [showAll, setShowAll] = useState(false);
  const [behind, setBehind] = useState(false);

  const total = buffer.size;
  const lines = buffer.lines;
  const shown = showAll ? lines : lines.slice(-WINDOW);
  const firstNumber = buffer.firstLineNumber + (lines.length - shown.length);

  // Follow the tail unless the reader scrolled up.
  useLayoutEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    if (stick.current) box.scrollTop = box.scrollHeight;
    else setBehind(true);
  });

  const onScroll = useCallback(() => {
    const box = boxRef.current;
    if (!box) return;
    stick.current = box.scrollHeight - box.scrollTop - box.clientHeight < 48;
    if (stick.current) setBehind(false);
  }, []);

  const jumpToEnd = () => {
    const box = boxRef.current;
    if (!box) return;
    stick.current = true;
    box.scrollTo({ top: box.scrollHeight, behavior: "smooth" });
    setBehind(false);
  };

  const st = STATE_LABEL[state];
  return (
    <div className="overflow-hidden rounded-2xl border border-white/10 bg-ink-950">
      <div className="flex items-center justify-between border-b border-white/10 bg-white/[0.03] px-4 py-2.5 text-xs">
        <span className="inline-flex items-center gap-2 text-slate-400">
          <span className="relative inline-flex h-2 w-2">
            {state === "live" && <span className="absolute inline-flex h-full w-full rounded-full bg-signal-ok animate-pulse-ring" />}
            <span className={clsx("relative inline-flex h-2 w-2 rounded-full", st.dot)} />
          </span>
          {st.text}
        </span>
        <span className="flex items-center gap-4 text-slate-500">
          {lines.length > WINDOW && (
            <button onClick={() => setShowAll((v) => !v)} className="text-accent-soft hover:underline">
              {showAll ? `Только последние ${WINDOW}` : `Показать все ${total}`}
            </button>
          )}
          <span>{buffer.nextSeq} строк</span>
        </span>
      </div>

      <div className="relative">
        <div ref={boxRef} onScroll={onScroll} className="h-[calc(100vh-29rem)] min-h-[28rem] max-h-[64rem] overflow-auto px-2 py-3 font-mono text-xs leading-5">
          {shown.length === 0 ? (
            <div className="px-3 py-2 text-slate-600">
              {state === "ended" ? "Лог пуст" : active ? "Ожидание вывода" : "Вывода нет"}
              {state !== "ended" && active && <span className="ml-0.5 inline-block w-2 animate-pulse bg-slate-500">&nbsp;</span>}
            </div>
          ) : (
            shown.map((text, i) => <Line key={firstNumber + i} number={firstNumber + i} text={text} />)
          )}
        </div>
        {behind && (
          <button
            onClick={jumpToEnd}
            className="absolute bottom-3 right-4 rounded-full border border-accent/40 bg-ink-900/90 px-3 py-1.5 text-xs text-accent-soft shadow-lg backdrop-blur transition hover:bg-accent/20"
          >
            ↓ к новым строкам
          </button>
        )}
      </div>
    </div>
  );
}
