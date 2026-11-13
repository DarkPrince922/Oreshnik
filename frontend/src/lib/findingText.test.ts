import { describe, expect, it } from "vitest";

import { asText, codeLocations, pick, stripFence } from "./findingText";

describe("asText", () => {
  it("turns any value into a string without interpreting it", () => {
    expect(asText("<img src=x onerror=alert(1)>")).toBe("<img src=x onerror=alert(1)>");
    expect(asText(null)).toBe("");
    expect(asText(undefined)).toBe("");
    expect(asText(5)).toBe("5");
    expect(asText(false)).toBe("false");
    expect(asText({ a: 1 })).toBe('{\n  "a": 1\n}');
  });
  it("survives values JSON cannot serialise", () => {
    const loop: Record<string, unknown> = {};
    loop.self = loop;
    expect(typeof asText(loop)).toBe("string");
  });
});

describe("stripFence", () => {
  it("removes one surrounding markdown fence", () => {
    expect(stripFence("```bash\ncurl -i x\n```")).toBe("curl -i x");
    expect(stripFence("```\nplain\n```")).toBe("plain");
  });
  it("handles longer fences that wrap shorter ones", () => {
    expect(stripFence("````md\n```js\nx\n```\n````")).toBe("```js\nx\n```");
  });
  it("leaves unfenced code alone", () => {
    expect(stripFence("echo hi")).toBe("echo hi");
  });
});

describe("codeLocations", () => {
  it("normalises locations and tolerates junk", () => {
    expect(codeLocations([{ file: "a.py", start_line: 3, end_line: 9, label: "sink", snippet: "x" }, { file: "b.js", start_line: 4 }, "junk", null])).toEqual([
      { file: "a.py", lines: "3–9", label: "sink", snippet: "x" },
      { file: "b.js", lines: "4", label: "", snippet: "" },
    ]);
    expect(codeLocations(undefined)).toEqual([]);
  });
});

describe("pick", () => {
  it("returns the first non-empty field", () => {
    expect(pick({ a: "", b: "  ", c: "yes" }, "a", "b", "c")).toBe("yes");
    expect(pick({}, "a")).toBe("");
  });
});
