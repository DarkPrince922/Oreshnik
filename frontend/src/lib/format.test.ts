import { describe, expect, it } from "vitest";

import { scanDuration, timesRu } from "./format";

const at = (iso: string) => new Date(iso).getTime();

describe("scanDuration", () => {
  const started = "2026-11-13T10:00:00Z";
  it("is the wall-clock run time of an ordinary scan, ticking while it runs", () => {
    expect(scanDuration({ started_at: started, finished_at: "2026-11-13T10:05:00Z" }, at("2026-11-14T00:00:00Z"))).toBe(300_000);
    expect(scanDuration({ started_at: started, finished_at: null }, at("2026-11-13T10:00:42Z"))).toBe(42_000);
    expect(scanDuration({ started_at: null, finished_at: null }, Date.now())).toBeNull();
  });

  it("adds the time of the earlier runs of a scan continued in place, but not the pauses between them", () => {
    // 10 minutes in the first run, left alone for hours, then 2 minutes in this run
    const scan = { elapsed_base: 600, started_at: "2026-11-13T20:00:00Z", finished_at: "2026-11-13T20:02:00Z" };
    expect(scanDuration(scan, at("2026-11-14T00:00:00Z"))).toBe(720_000);
    expect(scanDuration({ ...scan, finished_at: null }, at("2026-11-13T20:01:00Z"))).toBe(660_000); // still running: banked + so far
  });

  it("shows the banked time while a continued scan waits in the queue (no start yet)", () => {
    expect(scanDuration({ elapsed_base: 600, started_at: null, finished_at: null }, Date.now())).toBe(600_000);
    expect(scanDuration({ elapsed_base: 0, started_at: null, finished_at: null }, Date.now())).toBeNull();
  });

  it("never goes negative on clock skew", () => {
    expect(scanDuration({ started_at: "2026-11-13T10:00:10Z", finished_at: null }, at("2026-11-13T10:00:00Z"))).toBe(0);
    expect(scanDuration({ elapsed_base: -5, started_at: started, finished_at: "2026-11-13T10:00:01Z" }, 0)).toBe(1000);
  });
});

describe("timesRu", () => {
  it("declines like Russian does", () => {
    expect(timesRu(1)).toBe("1 раз");
    expect(timesRu(2)).toBe("2 раза");
    expect(timesRu(4)).toBe("4 раза");
    expect(timesRu(5)).toBe("5 раз");
    expect(timesRu(11)).toBe("11 раз");
    expect(timesRu(12)).toBe("12 раз");
    expect(timesRu(14)).toBe("14 раз");
    expect(timesRu(21)).toBe("21 раз");
    expect(timesRu(22)).toBe("22 раза");
    expect(timesRu(100)).toBe("100 раз");
  });
});
