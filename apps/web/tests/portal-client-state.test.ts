import { describe, expect, it, vi } from "vitest";

import {
  advancePortalDraftToReview,
  portalDraftFieldsEqual,
  type SavePortalDraft,
  type StartPortalReview,
} from "../src/features/sandbox/portal-client";
import type {
  PortalDraftFields,
  PortalView,
} from "../src/features/sandbox/contracts";

const FIELDS: PortalDraftFields = {
  attachments: [
    `model-${"1".repeat(32)}.png`,
    `model-${"2".repeat(32)}.png`,
    `model-${"3".repeat(32)}.png`,
  ],
  claimantName: "Demo Claimant",
  counterpartyKnown: "yes",
  incidentDate: "2026-07-14",
  incidentTime: "14:30:00",
  location: "Demo Street 1, Berlin",
  narrative: "A staged incident.",
  policyReference: "DEMO-POLICY-001",
  vehicleRegistration: "DEMO-CD-1",
};

function portalView(version: number, fields: PortalDraftFields = FIELDS): PortalView {
  return {
    auditCount: version,
    caseId: "case-portal-client-state",
    contractVersion: "4.0.0",
    fields,
    state: version >= 3 ? "review" : "draft",
    updatedAt: "2026-07-15T12:00:00Z",
    variant: "A",
    version,
  };
}

describe("portal client exact-once review transition", () => {
  it("skips a second save when the loaded draft is byte-identical", async () => {
    const saved = portalView(2);
    const reviewed = portalView(3);
    const save = vi.fn<SavePortalDraft>();
    const review = vi
      .fn<StartPortalReview>()
      .mockResolvedValue(reviewed);

    await expect(
      advancePortalDraftToReview(
        saved.caseId,
        saved.variant,
        saved,
        { ...FIELDS, attachments: [...FIELDS.attachments] },
        save,
        review,
      ),
    ).resolves.toBe(reviewed);

    expect(save).not.toHaveBeenCalled();
    expect(review).toHaveBeenCalledOnce();
    expect(review).toHaveBeenCalledWith(saved.caseId, saved.variant, 2);
  });

  it("saves one dirty draft before review and preserves ordered-byte equality", async () => {
    const initial = portalView(1, { ...FIELDS, location: "Old location" });
    const edited = { ...FIELDS, attachments: [...FIELDS.attachments] };
    const saved = portalView(2, edited);
    const reviewed = portalView(3, edited);
    const save = vi.fn<SavePortalDraft>().mockResolvedValue(saved);
    const review = vi
      .fn<StartPortalReview>()
      .mockResolvedValue(reviewed);

    await expect(
      advancePortalDraftToReview(
        initial.caseId,
        initial.variant,
        initial,
        edited,
        save,
        review,
      ),
    ).resolves.toBe(reviewed);

    expect(save).toHaveBeenCalledOnce();
    expect(save).toHaveBeenCalledWith(
      initial.caseId,
      initial.variant,
      1,
      edited,
    );
    expect(review).toHaveBeenCalledWith(initial.caseId, initial.variant, 2);
    expect(portalDraftFieldsEqual(initial.fields, edited)).toBe(false);
    expect(
      portalDraftFieldsEqual(edited, {
        ...edited,
        attachments: [edited.attachments[1]!, edited.attachments[0]!, edited.attachments[2]!],
      }),
    ).toBe(false);
  });

  it("treats a whitespace-only scalar delta as dirty and forwards it unchanged", async () => {
    const initial = portalView(1);
    const edited = {
      ...FIELDS,
      narrative: `${FIELDS.narrative} `,
      attachments: [...FIELDS.attachments],
    };
    const saved = portalView(2, edited);
    const reviewed = portalView(3, edited);
    const save = vi.fn<SavePortalDraft>().mockResolvedValue(saved);
    const review = vi.fn<StartPortalReview>().mockResolvedValue(reviewed);

    await expect(
      advancePortalDraftToReview(
        initial.caseId,
        initial.variant,
        initial,
        edited,
        save,
        review,
      ),
    ).resolves.toBe(reviewed);

    expect(portalDraftFieldsEqual(initial.fields, edited)).toBe(false);
    expect(save).toHaveBeenCalledOnce();
    expect(save).toHaveBeenCalledWith(initial.caseId, initial.variant, 1, edited);
    expect(save.mock.calls[0]?.[3].narrative).toBe(`${FIELDS.narrative} `);
    expect(review).toHaveBeenCalledWith(initial.caseId, initial.variant, 2);
  });
});
