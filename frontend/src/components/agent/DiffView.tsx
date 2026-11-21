import clsx from "clsx";
import { useState } from "react";

import { changeLines, diffStats, parseDiff, type DiffLine } from "../../lib/diff";

const LINE_CLASS: Record<DiffLine["kind"], string> = {
  add: "bg-emerald-500/10 text-emerald-200",
  del: "bg-rose-500/10 text-rose-200",
  hunk: "text-sky-300/70",
  context: "text-slate-400",
  meta: "text-slate-600",
};
const MARK: Record<DiffLine["kind"], string> = { add: "+", del: "−", hunk: " ", context: " ", meta: " " };
const SHOWN = 300;

function Lines({ lines }: { lines: DiffLine[] }) {
  const [all, setAll] = useState(false);
  const shown = all ? lines : lines.slice(0, SHOWN);
  return (
    <div className="overflow-x-auto rounded-lg bg-black/40 py-1 font-mono text-[11px] leading-relaxed">
      {shown.map((line, i) => (
        <div key={i} className={clsx("flex whitespace-pre px-3", LINE_CLASS[line.kind])}>
          <span className="w-4 shrink-0 select-none opacity-60">{MARK[line.kind]}</span>
          <span>{line.text || " "}</span>
        </div>
      ))}
      {!all && lines.length > SHOWN && (
        <button type="button" className="px-3 py-1 text-slate-500 underline decoration-dotted hover:text-slate-300" onClick={() => setAll(true)}>
          Показать ещё {lines.length - SHOWN} строк
        </button>
      )}
    </div>
  );
}

export function Counts({ added, removed }: { added: number; removed: number }) {
  return (
    <span className="font-mono text-[11px]">
      <span className="text-emerald-300">+{added}</span> <span className="text-rose-300">−{removed}</span>
    </span>
  );
}

/** One file of a change: its name, how much changed, and (on click) the lines. */
export function FileChange({ path, kind, lines, open: initiallyOpen = false }: { path: string; kind: string; lines: DiffLine[]; open?: boolean }) {
  const [open, setOpen] = useState(initiallyOpen);
  const added = lines.filter((l) => l.kind === "add").length;
  const removed = lines.filter((l) => l.kind === "del").length;
  return (
    <div className="rounded-lg border border-white/5">
      <button type="button" className="flex w-full items-center gap-2 px-3 py-1.5 text-left" onClick={() => setOpen((o) => !o)}>
        <span className="w-3 text-[10px] text-slate-500">{open ? "▾" : "▸"}</span>
        <span className="min-w-0 flex-1 truncate font-mono text-xs text-slate-200">{path}</span>
        <span className="text-[10px] uppercase tracking-wide text-slate-500">{{ add: "новый", delete: "удалён", update: "изменён", added: "новый", deleted: "удалён", modified: "изменён", renamed: "переименован" }[kind] ?? kind}</span>
        <Counts added={added} removed={removed} />
      </button>
      {open && (
        <div className="px-2 pb-2">
          <Lines lines={lines} />
        </div>
      )}
    </div>
  );
}

/** A whole turn's `git diff`: every file it touched, with the totals. */
export function TurnDiff({ diff, live }: { diff: string; live: boolean }) {
  const files = parseDiff(diff);
  const total = diffStats(files);
  if (files.length === 0) return null;
  return (
    <div className="max-w-[92%] space-y-1.5 rounded-xl border border-white/5 bg-ink-900/50 p-3">
      <div className="flex items-center gap-2 text-xs text-slate-400">
        <span>Изменения за ход{live ? " (пока пишется)" : ""}</span>
        <span className="text-slate-600">·</span>
        <span>файлов: {total.files}</span>
        <Counts added={total.added} removed={total.removed} />
      </div>
      {files.map((f) => (
        <FileChange key={f.path} path={f.path} kind={f.status} lines={f.lines.filter((l) => l.kind !== "meta")} open={files.length <= 2 && f.lines.length <= 60} />
      ))}
    </div>
  );
}

export { changeLines };
