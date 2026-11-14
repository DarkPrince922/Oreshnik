import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Renders a Strix report. The markdown is written by an LLM that read the scanned target, so it is
 * attacker-influenced. react-markdown never emits raw HTML (no rehype-raw here) and strips unsafe URL
 * schemes such as `javascript:`; on top of that:
 *   - images are removed entirely (an <img> would make the viewer's browser call an arbitrary host),
 *   - links open in a new tab without referrer/opener.
 */
const components: Components = {
  h1: (p) => <h1 className="mb-4 mt-2 text-2xl font-semibold tracking-tight text-white" {...strip(p)} />,
  h2: (p) => <h2 className="mb-3 mt-8 border-b border-white/10 pb-2 text-lg font-semibold text-white" {...strip(p)} />,
  h3: (p) => <h3 className="mb-2 mt-6 text-base font-semibold text-slate-100" {...strip(p)} />,
  h4: (p) => <h4 className="mb-2 mt-4 text-sm font-semibold text-slate-200" {...strip(p)} />,
  p: (p) => <p className="my-3 leading-relaxed text-slate-300 [overflow-wrap:anywhere]" {...strip(p)} />,
  ul: (p) => <ul className="my-3 list-disc space-y-1.5 pl-6 text-slate-300" {...strip(p)} />,
  ol: (p) => <ol className="my-3 list-decimal space-y-1.5 pl-6 text-slate-300" {...strip(p)} />,
  li: (p) => <li className="[overflow-wrap:anywhere]" {...strip(p)} />,
  strong: (p) => <strong className="font-semibold text-white" {...strip(p)} />,
  blockquote: (p) => <blockquote className="my-4 border-l-2 border-accent/50 pl-4 text-slate-400" {...strip(p)} />,
  hr: () => <hr className="my-6 border-white/10" />,
  code: ({ className, children }) =>
    className ? (
      <code className="font-mono text-xs text-slate-200">{children}</code>
    ) : (
      <code className="rounded bg-white/10 px-1.5 py-0.5 font-mono text-[0.85em] text-accent-soft [overflow-wrap:anywhere]">{children}</code>
    ),
  pre: (p) => <pre className="my-4 whitespace-pre-wrap rounded-xl border border-white/10 bg-ink-950 p-4 text-xs [overflow-wrap:anywhere]" {...strip(p)} />,
  table: (p) => (
    <div className="my-4 overflow-x-auto rounded-xl border border-white/10">
      <table className="w-full text-left text-sm" {...strip(p)} />
    </div>
  ),
  th: (p) => <th className="border-b border-white/10 bg-white/5 px-3 py-2 font-medium text-slate-200" {...strip(p)} />,
  td: (p) => <td className="border-b border-white/5 px-3 py-2 text-slate-300 [overflow-wrap:anywhere]" {...strip(p)} />,
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer nofollow" className="text-accent-soft underline decoration-accent/40 underline-offset-2 hover:decoration-accent">
      {children}
    </a>
  ),
};

/** react-markdown passes the hast `node` prop along; it must not reach the DOM. */
function strip<T extends { node?: unknown }>(props: T): Omit<T, "node"> {
  const { node: _node, ...rest } = props;
  return rest;
}

export default function SafeMarkdown({ children }: { children: string }) {
  return (
    <div className="text-sm">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components} disallowedElements={["img"]} unwrapDisallowed>
        {children}
      </ReactMarkdown>
    </div>
  );
}
