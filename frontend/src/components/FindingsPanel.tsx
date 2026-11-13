import clsx from "clsx";
import { AnimatePresence, motion } from "framer-motion";
import { useState } from "react";

import { errorMessage, getFinding, type Finding, type FindingDetail } from "../lib/api";
import { asText, codeLocations, pick, stripFence } from "../lib/findingText";
import { formatDateTime } from "../lib/format";
import { SeverityChip } from "./scan";
import { ErrorNote, Spinner, rise, stagger } from "./ui";

/** Plain text, whitespace preserved. Never HTML: finding text is attacker-influenced. */
function Prose({ text }: { text: string }) {
  return <p className="whitespace-pre-wrap text-sm leading-relaxed text-slate-300 [overflow-wrap:anywhere]">{text}</p>;
}

function Code({ text }: { text: string }) {
  return (
    <pre className="overflow-x-auto rounded-xl border border-white/10 bg-ink-950 p-4 font-mono text-xs leading-relaxed text-slate-200">
      {text}
    </pre>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h4 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-slate-500">{title}</h4>
      {children}
    </section>
  );
}

function Meta({ label, value }: { label: string; value: string | number | null | undefined }) {
  if (value === null || value === undefined || value === "") return null;
  return (
    <span className="inline-flex items-center gap-1.5 rounded-md border border-white/10 bg-white/5 px-2 py-0.5 text-[11px] text-slate-400">
      <span className="text-slate-600">{label}</span>
      <span className="font-mono text-slate-300 [overflow-wrap:anywhere]">{value}</span>
    </span>
  );
}

function Detail({ finding }: { finding: FindingDetail }) {
  const d = finding.data;
  const description = pick(d, "description");
  const impact = pick(d, "impact");
  const evidence = pick(d, "evidence");
  const analysis = pick(d, "technical_analysis");
  const pocText = pick(d, "poc_description");
  const pocCode = pick(d, "poc_script_code");
  const fix = pick(d, "remediation_steps", "remediation");
  const counter = pick(d, "counterevidence");
  const locations = codeLocations(d.code_locations);

  return (
    <div className="space-y-5 border-t border-white/5 px-5 pb-5 pt-4">
      <div className="flex flex-wrap gap-1.5">
        <Meta label="CWE" value={finding.cwe} />
        <Meta label="CVE" value={finding.cve} />
        <Meta label="уверенность" value={pick(d, "confidence")} />
        <Meta label="сложность исправления" value={pick(d, "fix_effort")} />
        <Meta label="найдено" value={finding.found_at ? formatDateTime(finding.found_at) : null} />
        <Meta label="id" value={finding.vuln_id} />
      </div>
      {description && <Section title="Описание"><Prose text={description} /></Section>}
      {impact && <Section title="Влияние"><Prose text={impact} /></Section>}
      {evidence && <Section title="Доказательства"><Prose text={evidence} /></Section>}
      {analysis && <Section title="Технический анализ"><Prose text={analysis} /></Section>}
      {(pocText || pocCode) && (
        <Section title="Proof of concept">
          {pocText && <Prose text={pocText} />}
          {pocCode && <div className={pocText ? "mt-3" : ""}><Code text={stripFence(pocCode)} /></div>}
        </Section>
      )}
      {locations.length > 0 && (
        <Section title="Места в коде">
          <div className="space-y-3">
            {locations.map((loc, i) => (
              <div key={i}>
                <div className="mb-1 font-mono text-xs text-slate-400 [overflow-wrap:anywhere]">
                  {loc.file}
                  {loc.lines && <span className="text-slate-600">:{loc.lines}</span>}
                  {loc.label && <span className="ml-2 font-sans text-slate-500">— {loc.label}</span>}
                </div>
                {loc.snippet && <Code text={loc.snippet} />}
              </div>
            ))}
          </div>
        </Section>
      )}
      {fix && <Section title="Как исправить"><Prose text={fix} /></Section>}
      {counter && <Section title="Контраргументы"><Prose text={counter} /></Section>}
      {!description && !impact && !evidence && !analysis && !fix && (
        <Prose text={asText(d) === "{}" ? "Подробностей нет." : asText(d)} />
      )}
    </div>
  );
}

function FindingCard({ scanId, finding }: { scanId: number; finding: Finding }) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<FindingDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const toggle = async () => {
    const next = !open;
    setOpen(next);
    if (next && !detail && !loading) {
      setLoading(true);
      setError(null);
      try {
        setDetail(await getFinding(scanId, finding.id));
      } catch (e) {
        setError(errorMessage(e));
      } finally {
        setLoading(false);
      }
    }
  };

  const where = [finding.method, finding.endpoint].filter(Boolean).join(" ");
  return (
    <motion.div variants={rise} className={clsx("glass overflow-hidden rounded-2xl transition-colors", open && "border-accent/30")}>
      <button onClick={toggle} aria-expanded={open} className="flex w-full items-start gap-4 px-5 py-4 text-left">
        <SeverityChip severity={finding.severity} />
        <span className="min-w-0 flex-1">
          <span className="block font-medium text-white [overflow-wrap:anywhere]">{finding.title}</span>
          {(where || finding.target) && (
            <span className="mt-1 block truncate font-mono text-xs text-slate-500">{[finding.target, where].filter(Boolean).join(" · ")}</span>
          )}
        </span>
        {finding.cvss !== null && <span className="shrink-0 font-mono text-xs text-slate-500">CVSS {finding.cvss}</span>}
        <motion.span animate={{ rotate: open ? 180 : 0 }} className="shrink-0 text-slate-500" aria-hidden>
          ▾
        </motion.span>
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.25 }} className="overflow-hidden">
            {loading && <div className="flex justify-center py-8"><Spinner /></div>}
            {error && <div className="px-5 pb-4"><ErrorNote message={error} /></div>}
            {detail && <Detail finding={detail} />}
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

export default function FindingsPanel({ scanId, findings }: { scanId: number; findings: Finding[] }) {
  return (
    <motion.div variants={stagger} initial="hidden" animate="show" className="space-y-3">
      {findings.map((f) => (
        <FindingCard key={f.id} scanId={scanId} finding={f} />
      ))}
    </motion.div>
  );
}
