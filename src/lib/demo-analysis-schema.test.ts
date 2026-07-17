import { describe, expect, it } from "vitest";

import { demoActivity } from "@/test/demo-fixtures";

import {
  AgentActivitySchema,
  DemoAnalyzeResponseSchema,
} from "./demo-analysis-schema";

const readyResult = {
  claim: {
    damage: "Front-left bumper damage",
    dateTime: "July 17, 2026 at 2:30 PM",
    location: "Alexanderplatz, Berlin",
    photoCount: 3,
    status: "ready",
    whatHappened: "Another car hit the front-left side while turning.",
  },
  status: "ready",
} as const;

describe("demo analysis schemas", () => {
  it("accepts a bounded observable activity trace", () => {
    expect(
      DemoAnalyzeResponseSchema.parse({
        activity: demoActivity,
        result: readyResult,
      }),
    ).toEqual({ activity: demoActivity, result: readyResult });
  });

  it("rejects raw reasoning and technical provider fields", () => {
    expect(
      AgentActivitySchema.safeParse({
        ...demoActivity,
        reasoning: "private chain of thought",
      }).success,
    ).toBe(false);
    expect(
      DemoAnalyzeResponseSchema.safeParse({
        activity: demoActivity,
        responseId: "provider-response-id",
        result: readyResult,
      }).success,
    ).toBe(false);
  });

  it("rejects non-contiguous activity sequences", () => {
    expect(
      AgentActivitySchema.safeParse({
        events: demoActivity.events.map((event, index) =>
          index === 2 ? { ...event, sequence: 7 } : event,
        ),
      }).success,
    ).toBe(false);
  });
});
