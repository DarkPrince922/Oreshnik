/**
 * Client-side buffer for the live scan log.
 *
 * The server numbers log lines (`seq`, gapless, 0-based) and the client may reconnect
 * and resume with `from=nextSeq`; frames can therefore be replayed or arrive twice.
 * The buffer makes that harmless: duplicates are dropped, order is preserved, memory is capped.
 */
export class LogBuffer {
  private items: string[] = [];
  /** Lines dropped from the front to respect the memory cap (so line numbers stay right). */
  private dropped = 0;
  private expectedSeq = 0;

  constructor(private readonly max = 50_000) {}

  /** Sequence number to resume from after a reconnect. */
  get nextSeq(): number {
    return this.expectedSeq;
  }
  get lines(): readonly string[] {
    return this.items;
  }
  /** 1-based number of `lines[0]` in the original log. */
  get firstLineNumber(): number {
    return this.dropped + 1;
  }
  get size(): number {
    return this.items.length;
  }

  /** Returns false when the frame was a duplicate and was ignored. */
  add(seq: number, line: string): boolean {
    if (seq < this.expectedSeq) return false;
    if (seq > this.expectedSeq) {
      // The server never skips, but don't let numbering silently drift if it ever does.
      this.items.push(`… пропущено строк: ${seq - this.expectedSeq} …`);
    }
    this.items.push(line);
    this.expectedSeq = seq + 1;
    if (this.items.length > this.max) {
      const overflow = this.items.length - this.max;
      this.items.splice(0, overflow);
      this.dropped += overflow;
    }
    return true;
  }
}

export type LineKind = "panel" | "error" | "finding" | "command" | "plain";

/** Cosmetic classification only — the text itself is always rendered as plain text. */
export function classifyLine(line: string): { kind: LineKind; severity?: string } {
  if (line.startsWith("[oreshnik]")) return { kind: "panel" };
  if (line.startsWith("$ ")) return { kind: "command" };
  const finding = /^Finding vuln-\d+ \[([A-Za-z]+)\]/.exec(line);
  if (finding) return { kind: "finding", severity: finding[1].toLowerCase() };
  if (/\b(error|failed|exception|traceback|fatal)\b/i.test(line)) return { kind: "error" };
  return { kind: "plain" };
}
