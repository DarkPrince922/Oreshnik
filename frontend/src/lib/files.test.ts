import { describe, expect, it } from "vitest";

import { formatBytes, joinPath, parentPath } from "./files";

describe("paths", () => {
  it("joins and goes up", () => {
    expect(joinPath("", "a.txt")).toBe("a.txt");
    expect(joinPath("docs/sub", "a.txt")).toBe("docs/sub/a.txt");
    expect(parentPath("docs/sub")).toBe("docs");
    expect(parentPath("docs")).toBe("");
    expect(parentPath("")).toBe("");
  });
});

describe("formatBytes", () => {
  it("is readable at every size", () => {
    expect(formatBytes(0)).toBe("0 Б");
    expect(formatBytes(900)).toBe("900 Б");
    expect(formatBytes(1536)).toBe("1.5 КБ");
    expect(formatBytes(200 * 1024)).toBe("200 КБ");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 МБ");
    expect(formatBytes(3 * 1024 * 1024 * 1024)).toBe("3.0 ГБ");
  });
});
