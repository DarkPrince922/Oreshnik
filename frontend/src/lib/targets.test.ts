import { expect, it } from "vitest";

import { parseTargets } from "./targets";

it("splits on newlines, spaces and commas and drops empties", () => {
  expect(parseTargets("https://a.com\n b.com,c.com  \n\n 10.0.0.1,")).toEqual(["https://a.com", "b.com", "c.com", "10.0.0.1"]);
  expect(parseTargets("   ")).toEqual([]);
  expect(parseTargets("")).toEqual([]);
});
