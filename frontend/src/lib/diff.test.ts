import { describe, expect, it } from "vitest";

import { diffStats, parseDiff } from "./diff";

const TURN = [
  "diff --git a/a.txt b/a.txt",
  "index 5626abf..e438487 100644",
  "--- a/a.txt",
  "+++ b/a.txt",
  "@@ -1,2 +1,2 @@",
  " keep",
  "-one",
  "+uno",
  "diff --git a/hello.txt b/hello.txt",
  "new file mode 100644",
  "--- /dev/null",
  "+++ b/hello.txt",
  "@@ -0,0 +1,2 @@",
  "+hi",
  "+there",
  "diff --git a/old.txt b/old.txt",
  "deleted file mode 100644",
  "--- a/old.txt",
  "+++ /dev/null",
  "@@ -1 +0,0 @@",
  "-bye",
  "",
].join("\n");

describe("parseDiff", () => {
  const files = parseDiff(TURN);
  it("splits a turn's diff into files with their kind and counts", () => {
    expect(files.map((f) => [f.path, f.status, f.added, f.removed])).toEqual([
      ["a.txt", "modified", 1, 1],
      ["hello.txt", "added", 2, 0],
      ["old.txt", "deleted", 0, 1],
    ]);
  });
  it("tells added, removed and unchanged lines apart and keeps hunk headers", () => {
    const lines = files[0].lines;
    expect(lines.filter((l) => l.kind === "hunk").map((l) => l.text)).toEqual(["@@ -1,2 +1,2 @@"]);
    expect(lines.filter((l) => l.kind !== "meta" && l.kind !== "hunk")).toEqual([
      { kind: "context", text: "keep" },
      { kind: "del", text: "one" },
      { kind: "add", text: "uno" },
    ]);
  });
  it("does not count the ---/+++ lines as changes", () => {
    expect(files[1].lines.filter((l) => l.kind === "meta").map((l) => l.text)).toContain("+++ b/hello.txt");
    expect(files[1].added).toBe(2);
  });
  it("totals", () => {
    expect(diffStats(files)).toEqual({ files: 3, added: 3, removed: 2 });
  });
});

describe("parseDiff edge cases", () => {
  it("is empty for nothing", () => {
    expect(parseDiff("")).toEqual([]);
    expect(diffStats([])).toEqual({ files: 0, added: 0, removed: 0 });
  });
  it("copes with a bare patch without git's header", () => {
    const [file] = parseDiff("@@ -1 +1 @@\n-a\n+b\n");
    expect(file.path).toBe("изменения");
    expect([file.added, file.removed]).toEqual([1, 1]);
  });
  it("marks a rename and keeps 'no newline' notes as meta", () => {
    const [file] = parseDiff("diff --git a/x.txt b/y.txt\n--- a/x.txt\n+++ b/y.txt\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n");
    expect(file.status).toBe("renamed");
    expect(file.lines.some((l) => l.kind === "meta" && l.text.startsWith("\\"))).toBe(true);
  });
  it("keeps text that merely looks like a header inside a hunk", () => {
    const [file] = parseDiff("diff --git a/n.md b/n.md\n--- a/n.md\n+++ b/n.md\n@@ -1 +1,2 @@\n a\n+--- not a header\n");
    expect(file.added).toBe(1);
  });
});

import { changeLines } from "./diff";

describe("changeLines", () => {
  it("an update is the hunks of a unified diff", () => {
    expect(changeLines("update", "@@ -1 +1 @@\n-one\n+uno\n")).toEqual([
      { kind: "hunk", text: "@@ -1 +1 @@" },
      { kind: "del", text: "one" },
      { kind: "add", text: "uno" },
    ]);
  });
  it("a new file is all additions, a deleted one all removals", () => {
    expect(changeLines("add", "hi\nthere\n")).toEqual([{ kind: "add", text: "hi" }, { kind: "add", text: "there" }]);
    expect(changeLines("delete", "bye\n")).toEqual([{ kind: "del", text: "bye" }]);
    expect(changeLines("add", "")).toEqual([]);
  });
  it("copes with a full git diff and with nothing", () => {
    expect(changeLines("update", "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-x\n+y\n").map((l) => l.kind)).toEqual(["hunk", "del", "add"]);
    expect(changeLines("update", "")).toEqual([]);
  });
});
