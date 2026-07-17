import { describe, expect, it } from "vitest";

import { createDemoReplay } from "@/test/demo-fixtures";

import {
  ComputerUseReplaySchema,
  DemoPortalHandoffSuccessSchema,
} from "./portal-handoff-schema";

const screenshotDataUrl = "data:image/png;base64,cG5n";

describe("Computer Use replay schemas", () => {
  it("accepts exactly the opened, two navigation, five field, and verified frames", () => {
    const replay = createDemoReplay(screenshotDataUrl);

    expect(ComputerUseReplaySchema.parse(replay)).toEqual(replay);
    expect(
      DemoPortalHandoffSuccessSchema.parse({
        replay,
        screenshotDataUrl,
        status: "prepared",
        submitted: false,
      }),
    ).toMatchObject({ status: "prepared", submitted: false });
  });

  it("rejects duplicate fields and non-contiguous frames", () => {
    const replay = createDemoReplay(screenshotDataUrl);
    const duplicate = replay.steps.map((step, index) =>
      index === 4 && step.kind === "field_filled"
        ? { ...step, field: "damage" as const }
        : step,
    );
    const skipped = replay.steps.map((step, index) =>
      index === 5 ? { ...step, sequence: 8 } : step,
    );

    expect(
      ComputerUseReplaySchema.safeParse({ ...replay, steps: duplicate }).success,
    ).toBe(false);
    expect(
      ComputerUseReplaySchema.safeParse({ ...replay, steps: skipped }).success,
    ).toBe(false);
  });

  it("rejects missing, reordered, or duplicated portal navigation", () => {
    const replay = createDemoReplay(screenshotDataUrl);
    const reversed = replay.steps.map((step, index) => {
      if (index === 1 && step.kind === "navigated") {
        return { ...step, destination: "incident_claim" as const };
      }
      if (index === 2 && step.kind === "navigated") {
        return { ...step, destination: "claims" as const };
      }
      return step;
    });
    const duplicated = replay.steps.map((step, index) =>
      index === 2 && step.kind === "navigated"
        ? { ...step, destination: "claims" as const }
        : step,
    );
    const missing = replay.steps.filter((_, index) => index !== 1);

    expect(
      ComputerUseReplaySchema.safeParse({ ...replay, steps: reversed }).success,
    ).toBe(false);
    expect(
      ComputerUseReplaySchema.safeParse({ ...replay, steps: duplicated }).success,
    ).toBe(false);
    expect(
      ComputerUseReplaySchema.safeParse({ ...replay, steps: missing }).success,
    ).toBe(false);
  });

  it("rejects a mismatched final screenshot or any submission claim", () => {
    const replay = createDemoReplay(screenshotDataUrl);

    expect(
      DemoPortalHandoffSuccessSchema.safeParse({
        replay,
        screenshotDataUrl: "data:image/png;base64,b3RoZXI=",
        status: "prepared",
        submitted: false,
      }).success,
    ).toBe(false);
    expect(
      DemoPortalHandoffSuccessSchema.safeParse({
        replay,
        screenshotDataUrl,
        status: "prepared",
        submitted: true,
      }).success,
    ).toBe(false);
  });
});
