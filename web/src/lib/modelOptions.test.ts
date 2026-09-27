/**
 * `/model`'s option list. The interesting cases are the ones where the list
 * would otherwise be wrong or empty: an override nothing knows about, and a
 * backend whose shortlist is deliberately empty.
 */

import { describe, expect, it } from "vitest";

import { buildModelOptions } from "./modelOptions";

describe("buildModelOptions", () => {
  it("leads with the agent's default, which is what clears the override", () => {
    const opts = buildModelOptions({
      sessionModel: null,
      agentModel: "sonnet",
      backendModels: ["opus", "sonnet", "haiku"],
    });
    expect(opts[0]).toMatchObject({
      value: null,
      label: "Agent default (sonnet)",
      current: true,
    });
    expect(opts.map((o) => o.value)).toEqual([null, "opus", "sonnet", "haiku"]);
  });

  it("marks the session's override as current, not the agent's", () => {
    const opts = buildModelOptions({
      sessionModel: "opus",
      agentModel: "sonnet",
      backendModels: ["opus", "sonnet"],
    });
    expect(opts.find((o) => o.current)?.value).toBe("opus");
    expect(opts[0].current).toBe(false);
  });

  it("includes a hand-typed model the shortlist has never heard of", () => {
    const opts = buildModelOptions({
      sessionModel: "some-new-model",
      agentModel: null,
      backendModels: ["opus"],
    });
    const found = opts.find((o) => o.value === "some-new-model");
    expect(found).toBeTruthy();
    expect(found?.current).toBe(true);
  });

  it("offers what is already in use when the shortlist is empty (Codex)", () => {
    const opts = buildModelOptions({
      sessionModel: null,
      agentModel: null,
      backendModels: [],
      inUse: ["gpt-5-codex", null, "gpt-5-codex", ""],
    });
    expect(opts.map((o) => o.value)).toEqual([null, "gpt-5-codex"]);
    expect(opts[0].label).toBe("Agent default");
  });

  it("never lists the same model twice", () => {
    const opts = buildModelOptions({
      sessionModel: "opus",
      agentModel: "opus",
      backendModels: ["opus", "opus"],
      inUse: ["opus"],
    });
    expect(opts.filter((o) => o.value === "opus")).toHaveLength(1);
  });
});
