import { describe, expect, it } from "vitest";

import { LogBuffer, classifyLine } from "./logStream";

describe("LogBuffer", () => {
  it("appends in order and reports the sequence to resume from", () => {
    const b = new LogBuffer();
    expect(b.nextSeq).toBe(0);
    b.add(0, "a");
    b.add(1, "b");
    expect(b.lines).toEqual(["a", "b"]);
    expect(b.nextSeq).toBe(2);
  });

  it("drops duplicates, e.g. after a reconnect replays frames", () => {
    const b = new LogBuffer();
    [0, 1, 2].forEach((i) => b.add(i, `l${i}`));
    expect(b.add(1, "l1")).toBe(false);
    expect(b.add(2, "l2")).toBe(false);
    expect(b.add(3, "l3")).toBe(true);
    expect(b.lines).toEqual(["l0", "l1", "l2", "l3"]);
  });

  it("marks a gap instead of silently shifting line numbers", () => {
    const b = new LogBuffer();
    b.add(0, "a");
    b.add(5, "f");
    expect(b.lines).toEqual(["a", "… пропущено строк: 4 …", "f"]);
    expect(b.nextSeq).toBe(6);
    expect(b.add(5, "f")).toBe(false);
  });

  it("caps memory and keeps line numbering correct", () => {
    const b = new LogBuffer(3);
    for (let i = 0; i < 10; i++) b.add(i, `l${i}`);
    expect(b.lines).toEqual(["l7", "l8", "l9"]);
    expect(b.firstLineNumber).toBe(8);
    expect(b.nextSeq).toBe(10);
    expect(b.add(9, "again")).toBe(false);
  });

  it("starts numbering at 1", () => {
    expect(new LogBuffer().firstLineNumber).toBe(1);
  });
});

describe("classifyLine", () => {
  it("recognises the panel's own lines, commands and findings", () => {
    expect(classifyLine("[oreshnik] Раннер был удалён").kind).toBe("panel");
    expect(classifyLine("$ strix -n -m quick").kind).toBe("command");
    expect(classifyLine("Finding vuln-0001 [CRITICAL] SQLi")).toEqual({ kind: "finding", severity: "critical" });
  });
  it("flags error-looking lines but not words that merely contain 'error'", () => {
    expect(classifyLine("Error: model connection failed").kind).toBe("error");
    expect(classifyLine("Traceback (most recent call last)").kind).toBe("error");
    expect(classifyLine("terrorists are not errors").kind).toBe("plain");
    expect(classifyLine("all good").kind).toBe("plain");
  });
});
