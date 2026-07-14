import { describe, expect, it } from "vitest";

import type {
  ClaimData,
  CounterpartyKnown,
  EvalInput,
  PortalState,
  RequiredClaimField,
} from "../../../contracts/generated/claimdone";
import {
  COUNTERPARTY_KNOWN_VALUES,
  PORTAL_FIELD_NAMES,
  PORTAL_STATES,
  PORTAL_VARIANTS,
  REQUIRED_CLAIM_FIELD_TO_PORTAL_FIELD,
  type PortalDraftFields,
  type PortalFieldName,
  type PortalVariant,
} from "../src/features/sandbox/contracts";
import { COMPLETE_PORTAL_FIELDS } from "../src/features/sandbox/fixtures";
import { demoAssetIdsForFiles } from "../src/features/sandbox/portal-client";
import { parsePortalFields, PortalInputError } from "../src/features/sandbox/validation";

type Equal<Left, Right> =
  (<Value>() => Value extends Left ? 1 : 2) extends <Value>() =>
    Value extends Right ? 1 : 2
    ? true
    : false;

function assertExact<Type extends true>(): Type {
  return true as Type;
}

// @ts-expect-error Portal states must come from the generated contract.
const inventedPortalState: PortalState = "submitted";
// @ts-expect-error Portal variants must come from the generated eval input contract.
const inventedPortalVariant: PortalVariant = "C";
// @ts-expect-error Drafts may be empty, but cannot invent a counterparty value.
const inventedCounterparty: PortalDraftFields["counterpartyKnown"] = "maybe";

void inventedPortalState;
void inventedPortalVariant;
void inventedCounterparty;

describe("sandbox portal contract coupling", () => {
  it("covers generated runtime unions exactly and without duplicates", () => {
    expect(assertExact<Equal<(typeof PORTAL_STATES)[number], PortalState>>()).toBe(true);
    expect(
      assertExact<Equal<(typeof PORTAL_VARIANTS)[number], EvalInput["portalVariant"]>>(),
    ).toBe(true);
    expect(
      assertExact<
        Equal<(typeof COUNTERPARTY_KNOWN_VALUES)[number], CounterpartyKnown>
      >(),
    ).toBe(true);

    expect(new Set(PORTAL_STATES).size).toBe(PORTAL_STATES.length);
    expect(new Set(PORTAL_VARIANTS).size).toBe(PORTAL_VARIANTS.length);
    expect(new Set(COUNTERPARTY_KNOWN_VALUES).size).toBe(
      COUNTERPARTY_KNOWN_VALUES.length,
    );
  });

  it("maps every generated required field to every portal draft field", () => {
    type FieldMap = typeof REQUIRED_CLAIM_FIELD_TO_PORTAL_FIELD;

    expect(assertExact<Equal<keyof FieldMap, RequiredClaimField>>()).toBe(true);
    expect(assertExact<Equal<FieldMap[RequiredClaimField], PortalFieldName>>()).toBe(true);
    expect(assertExact<Equal<(typeof PORTAL_FIELD_NAMES)[number], PortalFieldName>>()).toBe(
      true,
    );
    expect(new Set(PORTAL_FIELD_NAMES).size).toBe(PORTAL_FIELD_NAMES.length);
  });

  it("derives editable values from ClaimData while allowing an incomplete draft", () => {
    expect(
      assertExact<
        Equal<
          Exclude<PortalDraftFields["counterpartyKnown"], "">,
          ClaimData["counterpartyKnown"]
        >
      >(),
    ).toBe(true);
    expect(
      assertExact<
        Equal<
          PortalDraftFields["attachments"][number],
          ClaimData["attachments"][number]
        >
      >(),
    ).toBe(true);
    expect(
      assertExact<
        Equal<PortalDraftFields["incidentDate"], NonNullable<ClaimData["incidentDate"]>>
      >(),
    ).toBe(true);
  });

  it("accepts closed server asset IDs unchanged", () => {
    const attachments = [
      "model-0123456789abcdef0123456789abcdef.jpg",
      "model-fedcba9876543210fedcba9876543210.png",
      "asset-demo-context",
    ];

    expect(parsePortalFields({ ...COMPLETE_PORTAL_FIELDS, attachments }).attachments).toEqual(
      attachments,
    );
  });

  it("creates deterministic closed demo IDs without exposing local file names", () => {
    const ids = demoAssetIdsForFiles([
      { name: "Rear Overview (Private).JPG" },
      { name: "Detail.png" },
      { name: "Übersicht.jpg" },
    ]);

    expect(ids).toEqual([
      "asset-demo-local-1-rear-overview-private",
      "asset-demo-local-2-detail",
      "asset-demo-local-3-bersicht",
    ]);
    expect(
      parsePortalFields({ ...COMPLETE_PORTAL_FIELDS, attachments: ids }).attachments,
    ).toEqual(ids);
  });

  it.each([
    ["path traversal", ["../model-0123456789abcdef0123456789abcdef.jpg"]],
    ["slash", ["folder/model-0123456789abcdef0123456789abcdef.jpg"]],
    ["control", ["asset-demo-context\u0000"]],
    ["empty", [""]],
    ["too long", [`asset-demo-${"a".repeat(130)}`]],
    ["wrong digest length", ["model-deadbeef.jpg"]],
    ["uppercase digest", ["model-ABCDEFABCDEFABCDEFABCDEFABCDEFAB.jpg"]],
    ["duplicate", ["asset-demo-context", "asset-demo-context"]],
  ])("rejects %s attachment references", (_label, attachments) => {
    expect(() => parsePortalFields({ ...COMPLETE_PORTAL_FIELDS, attachments })).toThrow(
      PortalInputError,
    );
  });
});
