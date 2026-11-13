import { describe, expect, it } from "vitest";

import { canCancel, canSubmit, fleetSummary, hostStatusLabel, runStatusLabel } from "./ops";

describe("canSubmit", () => {
  it("needs a goal, a profile and at least one runner", () => {
    expect(canSubmit({ goal: "", llmProfileId: 1, runnerIds: [1] })).toBe(false);
    expect(canSubmit({ goal: "   ", llmProfileId: 1, runnerIds: [1] })).toBe(false);
    expect(canSubmit({ goal: "do it", llmProfileId: null, runnerIds: [1] })).toBe(false);
    expect(canSubmit({ goal: "do it", llmProfileId: 1, runnerIds: [] })).toBe(false);
    expect(canSubmit({ goal: "do it", llmProfileId: 1, runnerIds: [1, 2] })).toBe(true);
  });

  it("refuses more runners than the server would accept", () => {
    const many = Array.from({ length: 51 }, (_, i) => i);
    expect(canSubmit({ goal: "do it", llmProfileId: 1, runnerIds: many })).toBe(false);
  });
});

describe("canCancel", () => {
  it("only while drafting or running, and only once", () => {
    expect(canCancel({ status: "drafting", cancel_requested: false })).toBe(true);
    expect(canCancel({ status: "running", cancel_requested: false })).toBe(true);
    expect(canCancel({ status: "running", cancel_requested: true })).toBe(false);
    expect(canCancel({ status: "done", cancel_requested: false })).toBe(false);
    expect(canCancel({ status: "failed", cancel_requested: false })).toBe(false);
  });
});

describe("fleetSummary", () => {
  it("counts hosts by status", () => {
    const hosts = [
      { id: 1, runner_id: 1, runner_name: "a", status: "succeeded" as const, exit_code: 0, output: "", started_at: null, finished_at: null },
      { id: 2, runner_id: 2, runner_name: "b", status: "succeeded" as const, exit_code: 0, output: "", started_at: null, finished_at: null },
      { id: 3, runner_id: 3, runner_name: "c", status: "failed" as const, exit_code: 1, output: "", started_at: null, finished_at: null },
    ];
    expect(fleetSummary(hosts)).toBe(`${hostStatusLabel("succeeded")}: 2, ${hostStatusLabel("failed")}: 1`);
  });

  it("says so when there are no hosts", () => {
    expect(fleetSummary([])).toBe("нет хостов");
  });
});

describe("labels", () => {
  it("cover every status (no missing key throws at module load)", () => {
    expect(runStatusLabel("drafting")).toBeTruthy();
    expect(runStatusLabel("done")).toBeTruthy();
    expect(hostStatusLabel("blocked")).toBeTruthy();
  });
});
