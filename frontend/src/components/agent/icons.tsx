import type { ReactNode } from "react";

/** Small stroke icons for the «Агент» page. Decorative: the button or label next to them carries the meaning. */
function Icon({ children, className = "h-4 w-4", strokeWidth = 1.8 }: { children: ReactNode; className?: string; strokeWidth?: number }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {children}
    </svg>
  );
}
type P = { className?: string };

export const PlusIcon = (p: P) => <Icon {...p} strokeWidth={2}><path d="M12 5v14M5 12h14" /></Icon>;
export const SearchIcon = (p: P) => <Icon {...p}><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></Icon>;
export const FolderIcon = (p: P) => <Icon {...p}><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" /></Icon>;
export const TargetIcon = (p: P) => <Icon {...p}><circle cx="12" cy="12" r="9" /><circle cx="12" cy="12" r="5" /><circle cx="12" cy="12" r="1" /></Icon>;
export const CompressIcon = (p: P) => <Icon {...p}><path d="M4 14h6v6M20 10h-6V4M14 10l7-7M3 21l7-7" /></Icon>;
export const ReviewIcon = (p: P) => <Icon {...p}><circle cx="12" cy="12" r="9" /><path d="m8.5 12 2.5 2.5 4.5-5" /></Icon>;
export const UndoIcon = (p: P) => <Icon {...p}><path d="M9 14 4 9l5-5" /><path d="M4 9h10a6 6 0 0 1 0 12h-3" /></Icon>;
export const BranchIcon = (p: P) => <Icon {...p}><circle cx="6" cy="5" r="2" /><circle cx="6" cy="19" r="2" /><circle cx="18" cy="8" r="2" /><path d="M6 7v10M18 10c0 5-12 2-12 7" /></Icon>;
export const SlidersIcon = (p: P) => <Icon {...p}><path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1" /><circle cx="15" cy="6" r="2" /><circle cx="9" cy="12" r="2" /><circle cx="17" cy="18" r="2" /></Icon>;
export const ArrowUpIcon = (p: P) => <Icon {...p} strokeWidth={2}><path d="M12 19V5M5 12l7-7 7 7" /></Icon>;
export const TerminalIcon = (p: P) => <Icon {...p} strokeWidth={2}><path d="m5 8 4 4-4 4M12 16h7" /></Icon>;
export const CloseIcon = (p: P) => <Icon {...p} strokeWidth={2}><path d="M6 6l12 12M18 6 6 18" /></Icon>;
