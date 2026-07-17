import { describe, expect, it } from "vitest";

import type { Claim } from "./analysis-schema";
import {
  getPortalFieldValues,
  normalizePortalFieldValue,
} from "./portal-field-values";

describe("portal field values", () => {
  it("canonicalizes typographic punctuation before the exact safety check", () => {
    expect(
      normalizePortalFieldValue(
        "  Driver’s door\u00a0– dented · July 17, 2026  ",
      ),
    ).toBe("Driver's door - dented - July 17, 2026");
  });

  it("uses the same canonical values for the prompt and portal fields", () => {
    const claim: Claim = {
      damage: "Driver’s door – dented",
      dateTime: "July 17, 2026 · 5:25 PM",
      location: "Alexanderplatz, Berlin",
      photoCount: 3,
      status: "ready",
      whatHappened: "Another car hit my vehicle while turning.",
    };

    expect(getPortalFieldValues(claim)).toEqual({
      attachedPhotos: "3 accident photos attached",
      damage: "Driver's door - dented",
      dateTime: "July 17, 2026 - 5:25 PM",
      location: "Alexanderplatz, Berlin",
      whatHappened: "Another car hit my vehicle while turning.",
    });
  });
});
