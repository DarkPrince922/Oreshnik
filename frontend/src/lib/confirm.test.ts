import { afterEach, describe, expect, it } from "vitest";
import { confirm, setConfirmListener, type ConfirmRequest } from "./confirm";

afterEach(() => setConfirmListener(null));

describe("confirmation queue", () => {
  it("settles each request once and presents the next in order", async () => {
    const shown: (ConfirmRequest | null)[] = [];
    setConfirmListener((req) => shown.push(req));
    const first = confirm("first");
    const second = confirm("second");
    expect(shown.map((req) => req?.message)).toEqual(["first"]);
    shown[0]?.resolve(false);
    shown[0]?.resolve(true);
    expect(shown.map((req) => req?.message)).toEqual(["first", "second"]);
    shown[1]?.resolve(true);
    await expect(Promise.all([first, second])).resolves.toEqual([false, true]);
    expect(shown[2]).toBeNull();
  });

  it("cancels both the displayed and queued requests when the host is removed", async () => {
    setConfirmListener(() => {});
    const answers = Promise.all([confirm("first"), confirm("second")]);
    setConfirmListener(null);
    await expect(answers).resolves.toEqual([false, false]);
  });
});
