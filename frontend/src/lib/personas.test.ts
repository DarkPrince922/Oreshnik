import { describe, expect, it } from "vitest";

import type { Persona } from "./api";
import { defaultPersona, personaInitial, personasFor } from "./personas";

const p = (id: number, name: string, over: Partial<Persona> = {}): Persona => ({
  id, name, description: "", instructions: "x", scope: "both", shared: false, is_default: false, mine: true, editable: true, sessions: 0,
  created_at: "", updated_at: "", ...over,
});

describe("personas", () => {
  const list = [p(1, "Пентестер"), p(2, "Аудитор", { shared: true, mine: false }), p(3, "DevOps", { scope: "agent" }), p(4, "Код-ревьюер", { scope: "code", is_default: true })];

  it("offers in a section only what fits it, own first, then by name", () => {
    expect(personasFor(list, "code").map((x) => x.id)).toEqual([4, 1, 2]);
    expect(personasFor(list, "agent").map((x) => x.id)).toEqual([1, 3, 2]); // Russian order: Cyrillic names before Latin ones
  });
  it("starts a new session with the user's own default, when it fits", () => {
    expect(defaultPersona(list, "code")?.id).toBe(4);
    expect(defaultPersona(list, "agent")).toBeNull();
    expect(defaultPersona([p(5, "Чужая", { is_default: true, mine: false })], "code")).toBeNull();
  });
  it("labels a badge with the first letter", () => {
    expect(personaInitial(" пентестер")).toBe("П");
    expect(personaInitial("")).toBe("?");
  });
});
