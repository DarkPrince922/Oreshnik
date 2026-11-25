/** A unified diff (`git diff` output, as Codex reports a turn's changes) split into files and lines, for the page to colour. */

export interface DiffLine {
  kind: "add" | "del" | "hunk" | "context" | "meta";
  text: string;
}
export interface DiffFile {
  /** The file as the diff names it (the new name; for a deleted file, the old one). */
  path: string;
  status: "added" | "deleted" | "modified" | "renamed";
  added: number;
  removed: number;
  lines: DiffLine[];
}

const PATH_RE = /^diff --git a\/(.+?) b\/(.+)$/;

export function parseDiff(diff: string): DiffFile[] {
  const files: DiffFile[] = [];
  let current: DiffFile | null = null;
  let inHunk = false;
  for (const raw of diff.split("\n")) {
    const header = PATH_RE.exec(raw);
    if (header) {
      current = { path: header[2], status: header[1] === header[2] ? "modified" : "renamed", added: 0, removed: 0, lines: [] };
      files.push(current);
      inHunk = false;
      continue;
    }
    if (!current) {
      // A diff without git's header (a bare patch): treat it as one anonymous file.
      if (raw.startsWith("@@") || raw.startsWith("+") || raw.startsWith("-")) {
        current = { path: "изменения", status: "modified", added: 0, removed: 0, lines: [] };
        files.push(current);
      } else {
        continue;
      }
    }
    if (raw.startsWith("@@")) {
      inHunk = true;
      current.lines.push({ kind: "hunk", text: raw });
    } else if (!inHunk) {
      // The lines between the `diff --git` line and the first hunk: mode, index, and the ---/+++ names.
      if (raw.startsWith("new file mode")) current.status = "added";
      else if (raw.startsWith("deleted file mode")) current.status = "deleted";
      else if (raw.startsWith("--- a/") && current.status === "deleted") current.path = raw.slice(6);
      if (raw !== "") current.lines.push({ kind: "meta", text: raw });
    } else if (raw.startsWith("+")) {
      current.added += 1;
      current.lines.push({ kind: "add", text: raw.slice(1) });
    } else if (raw.startsWith("-")) {
      current.removed += 1;
      current.lines.push({ kind: "del", text: raw.slice(1) });
    } else if (raw.startsWith("\\")) {
      current.lines.push({ kind: "meta", text: raw });
    } else if (raw !== "" || current.lines.length) {
      current.lines.push({ kind: "context", text: raw.startsWith(" ") ? raw.slice(1) : raw });
    }
  }
  // the split leaves one empty context line at the very end of the last file
  for (const file of files) {
    while (file.lines.length && file.lines[file.lines.length - 1].kind === "context" && file.lines[file.lines.length - 1].text === "") file.lines.pop();
  }
  return files;
}

export function diffStats(files: DiffFile[]): { files: number; added: number; removed: number } {
  return { files: files.length, added: files.reduce((n, f) => n + f.added, 0), removed: files.reduce((n, f) => n + f.removed, 0) };
}

/** The lines of one file's change as Codex reports it inside a `file_change` item: for an update, the hunks of a unified diff; for a new
 * file, just its content; for a deleted one, the content that was removed. Returned as coloured lines. */
export function changeLines(kind: string, diff: string): DiffLine[] {
  const body = diff.replace(/\n$/, "");
  if (kind === "add") return body === "" ? [] : body.split("\n").map((text) => ({ kind: "add" as const, text }));
  if (kind === "delete") return body === "" ? [] : body.split("\n").map((text) => ({ kind: "del" as const, text }));
  const files = parseDiff(diff.startsWith("diff --git") ? diff : `diff --git a/x b/x\n${diff}`);
  return files[0]?.lines.filter((l) => l.kind !== "meta") ?? [];
}
