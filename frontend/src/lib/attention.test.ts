import { describe, expect, it } from "vitest";

import type { Attention } from "./api";
import { lights, readSeen, sectionOf, writeSeen } from "./attention";

const att = (scans: Partial<Attention["scans"]> = {}, agent: Partial<Attention["agent"]> = {}): Attention => ({
  now: "2026-11-14T10:00:00Z",
  scans: { finished: 0, failed: 0, findings: 0, ...scans },
  agent: { finished: 0, waiting: 0, ...agent },
});

describe("sectionOf", () => {
  it("knows the sections and their inner pages", () => {
    expect(sectionOf("/scans")).toBe("scans");
    expect(sectionOf("/scans/12")).toBe("scans");
    expect(sectionOf("/scans/new")).toBe("scans");
    expect(sectionOf("/agent")).toBe("agent");
    expect(sectionOf("/")).toBeNull();
    expect(sectionOf("/scansomething")).toBeNull();
  });
});

describe("lights", () => {
  it("nothing new, nothing lit", () => {
    expect(lights(att(), null)).toEqual({});
    expect(lights(null, null)).toEqual({});
  });
  it("finished scans light up, coloured by the worst outcome", () => {
    expect(lights(att({ finished: 1 }), null).scans).toEqual({ tone: "ok", pulse: false, title: "1 скан завершён" });
    expect(lights(att({ finished: 3, findings: 2 }), null).scans).toMatchObject({ tone: "warn", title: "3 скана завершено, с находками: 2" });
    expect(lights(att({ finished: 5, findings: 1, failed: 1 }), null).scans).toMatchObject({ tone: "bad", title: "5 сканов завершено, с находками: 1, с ошибкой: 1" });
    expect(lights(att({ finished: 21 }), null).scans?.title).toBe("21 скан завершён");
    expect(lights(att({ finished: 11 }), null).scans?.title).toBe("11 сканов завершено");
  });
  it("the agent's light pulses while it waits for an answer", () => {
    expect(lights(att({}, { finished: 1 }), null).agent).toEqual({ tone: "accent", pulse: false, title: "агент закончил" });
    expect(lights(att({}, { waiting: 1 }), null).agent).toEqual({ tone: "warn", pulse: true, title: "агент ждёт вашего ответа" });
    expect(lights(att({}, { waiting: 2, finished: 3 }), null).agent).toMatchObject({ tone: "warn", pulse: true, title: "агент ждёт вашего ответа (2), агент закончил (3)" });
  });
  it("the section being looked at has no light; the other one keeps its own", () => {
    const both = att({ finished: 1 }, { waiting: 1 });
    expect(Object.keys(lights(both, "scans"))).toEqual(["agent"]);
    expect(Object.keys(lights(both, "agent"))).toEqual(["scans"]);
    expect(Object.keys(lights(both, null)).sort()).toEqual(["agent", "scans"]);
  });
});

describe("seen", () => {
  const memory = () => {
    const data = new Map<string, string>();
    return { getItem: (k: string) => data.get(k) ?? null, setItem: (k: string, v: string) => void data.set(k, v), data };
  };
  it("is remembered per account and per section", () => {
    const store = memory();
    writeSeen(1, "scans", "2026-11-14T10:00:00Z", store);
    expect(readSeen(1, "scans", store)).toBe("2026-11-14T10:00:00Z");
    expect(readSeen(1, "agent", store)).toBeNull();
    expect(readSeen(2, "scans", store)).toBeNull();
  });
  it("ignores a broken value and a storage that throws", () => {
    const store = memory();
    store.data.set("oreshnik_seen_1_scans", "not a date");
    expect(readSeen(1, "scans", store)).toBeNull();
    const broken = { getItem: () => { throw new Error("denied"); }, setItem: () => { throw new Error("denied"); } };
    expect(readSeen(1, "scans", broken)).toBeNull();
    expect(() => writeSeen(1, "scans", "2026-11-14T10:00:00Z", broken)).not.toThrow();
    expect(readSeen(1, "scans", null)).toBeNull();
  });
});
