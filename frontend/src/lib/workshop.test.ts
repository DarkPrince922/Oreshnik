import { describe, expect, it } from "vitest";

import type { McpServer, Skill } from "./api";
import { composeSkillMd, effectiveMcp, skillsForPersona, effectiveSkills, insertSkill, matchSkills, skillMention, splitSkillMd } from "./workshop";

const skill = (name: string, over: Partial<Skill> = {}): Skill => ({
  id: 1, name, description: `про ${name}`, skill_md: "", files: [], scope: "both", shared: false, enabled: true, mine: true, editable: true,
  overridden: false, edit_revision: "", notes: [], created_at: "", updated_at: "", ...over,
});

describe("what an agent gets", () => {
  it("skips disabled, replaced and other-agent skills", () => {
    const list = [
      skill("b"), skill("a"), skill("off", { enabled: false }),
      skill("a", { shared: true, overridden: true }), skill("agent", { scope: "agent" }),
    ];
    expect(effectiveSkills(list, "code").map((s) => s.name)).toEqual(["a", "b"]);
    expect(effectiveSkills(list, "agent").map((s) => s.name)).toEqual(["a", "agent", "b"]);
    expect(effectiveSkills([
      skill("fallback", { enabled: false }), skill("fallback", { shared: true, overridden: true }),
      skill("scoped", { scope: "agent" }), skill("scoped", { shared: true, overridden: true }),
    ], "code").every((s) => s.shared)).toBe(true);
    expect(effectiveMcp([{ ...skill("x"), last_check: null } as unknown as McpServer], "code")).toHaveLength(1);
  });
});

describe("a persona's own skills", () => {
  it("gives exactly the chosen ones, disabled too, and the own one over a shared one", () => {
    const list = [skill("a"), skill("off", { enabled: false }), skill("x", { shared: true, id: 9 }), skill("x", { id: 10 }), skill("agent", { scope: "agent" })];
    expect(skillsForPersona(list, { skills: ["off", "x", "agent"] }, "code").map((s) => [s.name, s.id])).toEqual([["off", 1], ["x", 10]]);
    expect(skillsForPersona(list, { skills: null }, "code").map((s) => s.name)).toEqual(["a", "x"]);
    expect(skillsForPersona(list, { skills: [] }, "code")).toEqual([]);
  });
});

describe("$ in the composer", () => {
  it("finds the mention being typed and replaces it", () => {
    expect(skillMention("проверь $jw", 11)).toEqual({ query: "jw", start: 8 });
    expect(skillMention("$", 1)).toEqual({ query: "", start: 0 });
    expect(skillMention("цена 5$", 7)).toBeNull(); // not after a space
    expect(skillMention("$jwt готово", 11)).toBeNull(); // the caret is past it
    expect(insertSkill("проверь $jw токен", 8, 11, "jwt-checks")).toEqual({ text: "проверь $jwt-checks токен", caret: 20 });
    expect(insertSkill("$", 0, 1, "recon")).toEqual({ text: "$recon ", caret: 7 });
  });
  it("ranks names that start with the query first", () => {
    const list = [skill("api-jwt"), skill("jwt-checks"), skill("recon", { description: "jwt внутри" }), skill("other")];
    expect(matchSkills(list, "jwt").map((s) => s.name)).toEqual(["jwt-checks", "api-jwt", "recon"]);
  });
});

describe("SKILL.md by fields", () => {
  it("splits and puts back, keeping other front-matter lines", () => {
    const md = "---\nname: jwt\ndescription: >\n  длинное\n  описание\nlicense: MIT\nmetadata:\n  a: b\n---\n\n# Тело\n";
    const f = splitSkillMd(md);
    expect(f).toEqual({ name: "jwt", description: "длинное описание", body: "# Тело\n", extra: "license: MIT\nmetadata:\n  a: b" });
    const back = splitSkillMd(composeSkillMd({ ...f, description: 'новое: "в кавычках"' }));
    expect(back.description).toBe('новое: "в кавычках"');
    expect(back.extra).toBe(f.extra);
    expect(splitSkillMd("просто текст")).toEqual({ name: "", description: "", body: "просто текст", extra: "" });
  });
});
