import { afterEach, describe, expect, it, vi } from "vitest";

import type { ClarificationAnswerRequest } from "../../../contracts/generated/claimdone";
import {
  answerClarification,
  answerThenRunToReview,
  ClaimDoneApiError,
  ClaimDonePendingCleanupError,
  claimDoneApiOrigin,
  claimDonePortalOrigin,
  createAndSubmitIntake,
  createCase,
  deleteAuthoritativeCase,
  isWorkflowSnapshotState,
  portalAReviewUrl,
  runClaimToReview,
  submitIntake,
  type ClaimDoneFetch,
  type ReviewResponse,
} from "../src/features/intake";
import {
  CLARIFICATION_SNAPSHOT,
  CREATED_SNAPSHOT,
  READY_SNAPSHOT,
  RECEIPT_SNAPSHOT,
  REPAIR_SNAPSHOT,
} from "../src/features/workflow/fixtures";
import { parseWorkflowSnapshot } from "../src/features/workflow/validation";

const CASE_ID = "case-happy-001";

afterEach(() => {
  delete process.env.NEXT_PUBLIC_CLAIMDONE_API_ORIGIN;
  delete process.env.NEXT_PUBLIC_CLAIMDONE_PORTAL_ORIGIN;
  vi.restoreAllMocks();
});

describe("canonical INT-002 intake API", () => {
  it("accepts only a canonical created WorkflowSnapshot", async () => {
    const fetcher = vi.fn<ClaimDoneFetch>(async (_input, init) => {
      expect(init).toMatchObject({ method: "POST" });
      expect(JSON.parse(String(init?.body))).toEqual({ metadata: {} });
      return Response.json(createdBody());
    });

    const created = await createCase(fetcher);

    expect(created.case.state).toBe("created");
    expect(created.case.version).toBe(1);
    expect(created.contractVersion).toBe("4.0.0");
    expect(created.receipt).toBeNull();
  });

  it.each([
    ["legacy CaseView", legacyCaseView()],
    ["legacy FlowResponse", legacyFlowResponse()],
    ["created snapshot at the wrong V1 version", wrongCreatedVersionBody()],
    ["forged created state", forgedCreatedSnapshot()],
  ])("rejects %s instead of accepting non-canonical authority", async (_label, body) => {
    const fetcher: ClaimDoneFetch = async () => Response.json(body);

    await expect(createCase(fetcher)).rejects.toMatchObject({
      detail: { code: "CLIENT_INVALID_RESPONSE" },
    });
  });

  it("submits the multipart intake and validates the canonical incident_time clarification", async () => {
    const body = int002ClarificationBody();
    expect(parseWorkflowSnapshot(body, CASE_ID).case.state).toBe(
      "awaiting_clarification",
    );
    const fetcher = vi.fn<ClaimDoneFetch>(async (input, init) => {
      expect(String(input)).toBe(
        "http://127.0.0.1:8000/api/cases/case-happy-001/intake",
      );
      expect(init?.method).toBe("POST");
      const form = init?.body;
      expect(form).toBeInstanceOf(FormData);
      const multipart = form as FormData;
      expect(multipart.get("expectedVersion")).toBe("1");
      expect(multipart.get("statementText")).toBe("  Staged Aussage.  ");
      expect(multipart.getAll("images")).toHaveLength(3);
      expect(multipart.getAll("exifDecisions")).toEqual([
        "strip",
        "retain",
        "strip",
      ]);
      return Response.json(body);
    });

    const response = await submitIntake(
      CASE_ID,
      {
        audio: null,
        dataProcessingApproved: true,
        exifDecisions: ["strip", "retain", "strip"],
        expectedVersion: 1,
        imageRightsConfirmed: true,
        images: demoImages(),
        sandboxAcknowledged: true,
        statementText: "  Staged Aussage.  ",
      },
      fetcher,
    );

    expect(response.case.state).toBe("awaiting_clarification");
    expect(response.clarification.field).toBe("incident_time");
    expect(response.clarification.round).toBe(1);
    expect(response.clarification.expectedVersion).toBe(response.case.version);
    expect(response.receipt).toBeNull();
  });

  it.each([
    ["wrong field", wrongFieldClarificationBody()],
    ["wrong V1 version", wrongVersionClarificationBody()],
  ])("rejects a canonical clarification snapshot with the %s", async (_label, body) => {
    const fetcher: ClaimDoneFetch = async () =>
      Response.json(body);

    await expect(
      submitIntake(CASE_ID, validIntakeSubmission(), fetcher),
    ).rejects.toMatchObject({ detail: { code: "CLIENT_INVALID_RESPONSE" } });
  });

  it("rejects an intake from a non-created V1 version before transport", async () => {
    const fetcher = vi.fn<ClaimDoneFetch>();

    await expect(
      submitIntake(
        CASE_ID,
        { ...validIntakeSubmission(), expectedVersion: 2 },
        fetcher,
      ),
    ).rejects.toMatchObject({ detail: { code: "CLIENT_INPUT_INVALID" } });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("posts the complete canonical answer request and preserves exact HH:MM:SS bytes", async () => {
    const request = clarificationRequest("14:30:00");
    const ready = readyBody();
    const fetcher = vi.fn<ClaimDoneFetch>(async (input, init) => {
      expect(String(input)).toBe(
        "http://127.0.0.1:8000/api/cases/case-happy-001/clarifications/clarification-001/answer",
      );
      expect(JSON.parse(String(init?.body))).toEqual(request);
      return Response.json(ready);
    });

    const response = await answerClarification(request, fetcher);

    expect(response.case.state).toBe("ready_to_fill");
    expect(response.case.version).toBe(5);
    expect(fetcher).toHaveBeenCalledOnce();
  });

  it.each(["14:30", "14:30:00 ", " 14:30:00", "24:00:00", "14:60:00"])(
    "rejects non-exact incident time %s before transport",
    async (answer) => {
      const fetcher = vi.fn<ClaimDoneFetch>();
      await expect(
        answerClarification(clarificationRequest(answer), fetcher),
      ).rejects.toMatchObject({ detail: { code: "CLIENT_INPUT_INVALID" } });
      expect(fetcher).not.toHaveBeenCalled();
    },
  );

  it("rejects an answer outside the canonical v4 clarification boundary", async () => {
    const fetcher = vi.fn<ClaimDoneFetch>();

    await expect(
      answerClarification(
        { ...clarificationRequest("14:30:00"), expectedVersion: 3 },
        fetcher,
      ),
    ).rejects.toMatchObject({ detail: { code: "CLIENT_INPUT_INVALID" } });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("rejects a malformed or wrong-state answer snapshot", async () => {
    const malformed = readyBody();
    record(malformed.case).version = 4;
    const fetcher: ClaimDoneFetch = async () => Response.json(malformed);

    await expect(
      answerClarification(clarificationRequest("14:30:00"), fetcher),
    ).rejects.toMatchObject({ detail: { code: "CLIENT_INVALID_RESPONSE" } });
  });

  it("runs only from the ready version and accepts verified G0-G8 with two attempts", async () => {
    const review = finalReviewBody();
    expect(parseWorkflowSnapshot(review, CASE_ID).case.state).toBe("review");
    const fetcher = vi.fn<ClaimDoneFetch>(async (input, init) => {
      expect(String(input)).toBe(
        "http://127.0.0.1:8000/api/cases/case-happy-001/run",
      );
      expect(JSON.parse(String(init?.body))).toEqual({
        contractVersion: "4.0.0",
        expectedVersion: 5,
      });
      return Response.json(review);
    });

    const response = await runClaimToReview(CASE_ID, 5, fetcher);

    expect(response.case.state).toBe("review");
    expect(response.case.version).toBe(9);
    expect(response.claimPacket.gateDecisions.map((gate) => gate.gateId)).toEqual([
      "G0",
      "G1",
      "G2",
      "G3",
      "G4",
      "G5",
      "G6",
      "G7",
      "G8",
    ]);
    expect(response.claimPacket.gateDecisions.every((gate) => gate.passed)).toBe(true);
    expect(response.verificationAttempts.attempts).toHaveLength(2);
    expect(response.verificationAttempts.attempts[0]).toMatchObject({
      attemptNumber: 1,
      final: false,
      report: { status: "mismatch" },
    });
    expect(response.verificationAttempts.attempts[0]?.repair).not.toBeNull();
    expect(response.verificationAttempts.attempts[1]).toMatchObject({
      attemptNumber: 2,
      final: true,
      report: { status: "verified" },
    });
    expect(response.receipt).toBeNull();
    expect(response.case.state).not.toBe("human_approved");
  });

  it("commits READY before run and retries a run failure without answering twice", async () => {
    const ready = parseWorkflowSnapshot(readyBody(), CASE_ID);
    if (!isWorkflowSnapshotState(ready, "ready_to_fill")) {
      throw new Error("Expected READY");
    }
    const review = parseWorkflowSnapshot(finalReviewBody(), CASE_ID);
    if (!isWorkflowSnapshotState(review, "review")) {
      throw new Error("Expected REVIEW");
    }
    const order: string[] = [];
    const answer = vi.fn(async () => {
      order.push("answer");
      return ready;
    });
    const run = vi
      .fn<(caseId: string, expectedVersion: number) => Promise<ReviewResponse>>()
      .mockImplementationOnce(async () => {
        order.push("run-failed");
        throw new Error("run unavailable");
      })
      .mockImplementationOnce(async () => {
        order.push("run-retry");
        return review;
      });
    const onReady = vi.fn(() => order.push("ready-committed"));

    await expect(
      answerThenRunToReview(clarificationRequest("14:30:00"), {
        answer,
        onReady,
        run,
      }),
    ).rejects.toThrow("run unavailable");
    expect(order).toEqual(["answer", "ready-committed", "run-failed"]);
    expect(answer).toHaveBeenCalledOnce();
    expect(run).toHaveBeenCalledWith(CASE_ID, 5);

    const retried = await run(ready.case.caseId, ready.case.version);
    expect(retried.case.state).toBe("review");
    expect(answer).toHaveBeenCalledOnce();
    expect(run).toHaveBeenCalledTimes(2);
    expect(order).toEqual([
      "answer",
      "ready-committed",
      "run-failed",
      "run-retry",
    ]);
  });

  it("rejects a run outside the canonical v5 READY boundary before transport", async () => {
    const fetcher = vi.fn<ClaimDoneFetch>();

    await expect(runClaimToReview(CASE_ID, 6, fetcher)).rejects.toMatchObject({
      detail: { code: "CLIENT_INPUT_INVALID" },
    });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it.each([
    ["one direct verification attempt", oneAttemptReviewBody()],
    ["Portal B", portalBReviewBody()],
    ["receipt/approval progression", structuredClone(RECEIPT_SNAPSHOT)],
    ["forged G8 pass", forgedG8ReviewBody()],
  ])("rejects final authority with %s", async (_label, body) => {
    const fetcher: ClaimDoneFetch = async () => Response.json(body);

    await expect(runClaimToReview(CASE_ID, 5, fetcher)).rejects.toMatchObject({
      detail: { code: "CLIENT_INVALID_RESPONSE" },
    });
  });

  it("rejects an otherwise canonical review snapshot outside V1 version 9", async () => {
    const body = finalReviewBody();
    record(body.case).version = 10;
    const fetcher: ClaimDoneFetch = async () => Response.json(body);

    await expect(runClaimToReview(CASE_ID, 5, fetcher)).rejects.toMatchObject({
      detail: { code: "CLIENT_INVALID_RESPONSE" },
    });
  });

  it("uses the exact local Portal A review route", () => {
    expect(portalAReviewUrl(CASE_ID)).toBe(
      "http://127.0.0.1:3000/sandbox/A/cases/case-happy-001",
    );
  });

  it("creates then submits, while exposing lifecycle ownership without legacy envelopes", async () => {
    const onCaseCreated = vi.fn();
    const onCaseCleaned = vi.fn();
    const fetcher = vi.fn<ClaimDoneFetch>(async (_input, init) =>
      init?.body instanceof FormData
        ? Response.json(int002ClarificationBody())
        : Response.json(createdBody()),
    );

    const result = await createAndSubmitIntake(
      newIntakeSubmission(),
      fetcher,
      { onCaseCleaned, onCaseCreated },
    );

    expect(result.case.state).toBe("awaiting_clarification");
    expect(onCaseCreated).toHaveBeenCalledWith(CASE_ID);
    expect(onCaseCleaned).not.toHaveBeenCalled();
  });

  it("preserves local cleanup ownership when intake and cleanup both fail", async () => {
    const fetcher = vi.fn<ClaimDoneFetch>(async (_input, init) => {
      if (init?.method === "DELETE") throw new Error("cleanup unavailable");
      if (init?.body instanceof FormData) return Response.json(legacyFlowResponse());
      return Response.json(createdBody());
    });

    await expect(
      createAndSubmitIntake(newIntakeSubmission(), fetcher),
    ).rejects.toBeInstanceOf(ClaimDonePendingCleanupError);
  });

  it("deletes the portal before the backend", async () => {
    const urls: string[] = [];
    const fetcher: ClaimDoneFetch = async (input, init) => {
      urls.push(String(input));
      expect(init?.method).toBe("DELETE");
      return new Response(null, { status: 204 });
    };

    await deleteAuthoritativeCase(CASE_ID, fetcher);

    expect(urls).toEqual([
      "http://127.0.0.1:3000/api/sandbox/cases/case-happy-001",
      "http://127.0.0.1:8000/api/cases/case-happy-001",
    ]);
  });

  it("retains the backend case when portal cleanup fails", async () => {
    const urls: string[] = [];
    const fetcher: ClaimDoneFetch = async (input) => {
      urls.push(String(input));
      throw new Error("portal cleanup unavailable");
    };

    await expect(deleteAuthoritativeCase(CASE_ID, fetcher)).rejects.toBeInstanceOf(
      ClaimDoneApiError,
    );
    expect(urls).toEqual([
      "http://127.0.0.1:3000/api/sandbox/cases/case-happy-001",
    ]);
  });

  it("retries idempotent portal cleanup before retrying a failed backend cleanup", async () => {
    const urls: string[] = [];
    let backendAttempts = 0;
    const fetcher: ClaimDoneFetch = async (input) => {
      const url = String(input);
      urls.push(url);
      if (url.startsWith("http://127.0.0.1:8000") && backendAttempts++ === 0) {
        throw new Error("backend cleanup unavailable");
      }
      return new Response(null, { status: 204 });
    };

    await expect(deleteAuthoritativeCase(CASE_ID, fetcher)).rejects.toBeInstanceOf(
      ClaimDoneApiError,
    );
    await expect(deleteAuthoritativeCase(CASE_ID, fetcher)).resolves.toBeUndefined();
    expect(urls).toEqual([
      "http://127.0.0.1:3000/api/sandbox/cases/case-happy-001",
      "http://127.0.0.1:8000/api/cases/case-happy-001",
      "http://127.0.0.1:3000/api/sandbox/cases/case-happy-001",
      "http://127.0.0.1:8000/api/cases/case-happy-001",
    ]);
  });

  it("preserves a closed ErrorEnvelope without accepting invented reason codes", async () => {
    const fetcher: ClaimDoneFetch = async () =>
      Response.json(
        {
          error: {
            code: "CASE_VERSION_CONFLICT",
            currentVersion: 9,
            fieldErrors: [
              {
                field: "expectedVersion",
                message: "Stale version.",
                reasonCode: "G6_STATE_INVALID",
              },
            ],
            message: "Version conflict.",
            reasonCodes: ["G6_STATE_INVALID"],
            gateDecision: null,
          },
        },
        { status: 409 },
      );

    await expect(runClaimToReview(CASE_ID, 5, fetcher)).rejects.toMatchObject({
      detail: {
        code: "CASE_VERSION_CONFLICT",
        currentVersion: 9,
        reasonCodes: ["G6_STATE_INVALID"],
      },
      status: 409,
    });

    const invented: ClaimDoneFetch = async () =>
      Response.json(
        {
          error: {
            code: "BAD",
            currentVersion: null,
            fieldErrors: [],
            message: "Bad.",
            reasonCodes: ["G8_INVENTED"],
            gateDecision: null,
          },
        },
        { status: 422 },
      );
    await expect(runClaimToReview(CASE_ID, 5, invented)).rejects.toMatchObject({
      detail: { code: "CLIENT_INVALID_RESPONSE" },
    });
  });

  it.each(malformedErrorEnvelopes())(
    "fails closed for a malformed ErrorEnvelope: %s",
    async (_label, body) => {
      const fetcher: ClaimDoneFetch = async () =>
        Response.json(body, { status: 422 });

      await expect(runClaimToReview(CASE_ID, 5, fetcher)).rejects.toMatchObject({
        detail: { code: "CLIENT_INVALID_RESPONSE" },
      });
    },
  );

  it("allows only the two exact loopback origins", () => {
    expect(claimDoneApiOrigin()).toBe("http://127.0.0.1:8000");
    expect(claimDonePortalOrigin()).toBe("http://127.0.0.1:3000");
    process.env.NEXT_PUBLIC_CLAIMDONE_API_ORIGIN = "http://localhost:8000";
    expect(() => claimDoneApiOrigin()).toThrow(ClaimDoneApiError);
    process.env.NEXT_PUBLIC_CLAIMDONE_PORTAL_ORIGIN = "http://127.0.0.1:3001";
    expect(() => claimDonePortalOrigin()).toThrow(ClaimDoneApiError);
  });
});

function demoImages(): [File, File, File] {
  return [1, 2, 3].map(
    (number) =>
      new File([`image-${number}`], `image-${number}.png`, {
        type: "image/png",
      }),
  ) as [File, File, File];
}

function validIntakeSubmission() {
  return {
    ...newIntakeSubmission(),
    expectedVersion: 1,
  };
}

function newIntakeSubmission() {
  return {
    audio: null,
    dataProcessingApproved: true,
    exifDecisions: ["strip", "retain", "strip"] as const,
    imageRightsConfirmed: true,
    images: demoImages(),
    sandboxAcknowledged: true,
    statementText: "Synthetic statement",
  };
}

function clarificationRequest(answer: string): ClarificationAnswerRequest {
  return {
    answer,
    caseId: CASE_ID,
    clarificationId: "clarification-001",
    contractVersion: "4.0.0",
    expectedVersion: 4,
    field: "incident_time",
    round: 1,
  };
}

function int002ClarificationBody() {
  const body = structuredClone(CLARIFICATION_SNAPSHOT) as MutableRecord;
  const packet = record(body.claimPacket);
  const claim = record(packet.claim);
  const readyClaim = record(record(structuredClone(READY_SNAPSHOT).claimPacket).claim);
  claim.incidentDate = readyClaim.incidentDate;
  claim.incidentTime = null;
  claim.missingRequiredFields = ["incident_time"];
  claim.fieldProvenance = array(readyClaim.fieldProvenance).filter(
    (entry) => record(entry).field !== "incident_time",
  );
  const clarification = record(body.clarification);
  record(body.case).version = 4;
  clarification.clarificationId = "clarification-001";
  clarification.field = "incident_time";
  clarification.question = "What time did the incident happen?";
  clarification.expectedVersion = 4;
  return body;
}

function wrongFieldClarificationBody() {
  const body = structuredClone(CLARIFICATION_SNAPSHOT) as MutableRecord;
  record(body.case).version = 4;
  record(body.clarification).expectedVersion = 4;
  return body;
}

function wrongVersionClarificationBody() {
  const body = int002ClarificationBody();
  record(body.case).version = 3;
  record(body.clarification).expectedVersion = 3;
  return body;
}

function readyBody() {
  const body = structuredClone(READY_SNAPSHOT) as MutableRecord;
  record(body.case).version = 5;
  record(body.case).updatedAt = "2026-07-14T12:00:21Z";
  body.requestId = "request-ready-int002";
  return body;
}

function finalReviewBody() {
  const body = structuredClone(REPAIR_SNAPSHOT) as MutableRecord;
  record(body.case).state = "review";
  record(body.case).version = 9;
  record(body.case).updatedAt = "2026-07-14T12:00:22Z";
  record(body.claimPacket).state = "review";
  body.requestId = "request-review-int002";
  return body;
}

function oneAttemptReviewBody() {
  const body = finalReviewBody();
  record(body.verificationAttempts).attempts = [
    array(record(body.verificationAttempts).attempts)[1],
  ];
  return body;
}

function portalBReviewBody() {
  const body = finalReviewBody();
  record(body.portalSession).variant = "B";
  return body;
}

function forgedG8ReviewBody() {
  const body = finalReviewBody();
  const gates = array(record(body.claimPacket).gateDecisions);
  record(gates[8]).deterministicPassed = false;
  return body;
}

function createdBody() {
  const body = structuredClone(CREATED_SNAPSHOT) as MutableRecord;
  record(body.case).version = 1;
  record(body.case).updatedAt = "2026-07-14T12:00:01Z";
  body.requestId = "request-created-int002";
  return body;
}

function wrongCreatedVersionBody() {
  const body = createdBody();
  record(body.case).version = 2;
  return body;
}

function canonicalErrorEnvelope() {
  return {
    error: {
      code: "CASE_VERSION_CONFLICT",
      currentVersion: 9,
      fieldErrors: [
        {
          field: "expectedVersion",
          message: "Stale version.",
          reasonCode: "G6_STATE_INVALID",
        },
      ],
      gateDecision: null,
      message: "Version conflict.",
      reasonCodes: ["G6_STATE_INVALID"],
    },
  } as MutableRecord;
}

function malformedErrorEnvelopes(): [string, unknown][] {
  const extraTop = canonicalErrorEnvelope();
  extraTop.unexpected = true;

  const extraDetail = canonicalErrorEnvelope();
  record(extraDetail.error).unexpected = true;

  const missingGateDecision = canonicalErrorEnvelope();
  delete record(missingGateDecision.error).gateDecision;

  const unsafeCode = canonicalErrorEnvelope();
  record(unsafeCode.error).code = "not-canonical";

  const unsafeMessage = canonicalErrorEnvelope();
  record(unsafeMessage.error).message = "Unsafe\u0000message";

  const tooLongMessage = canonicalErrorEnvelope();
  record(tooLongMessage.error).message = "x".repeat(513);

  const duplicateReasons = canonicalErrorEnvelope();
  record(duplicateReasons.error).reasonCodes = [
    "G6_STATE_INVALID",
    "G6_STATE_INVALID",
  ];

  const unsafeField = canonicalErrorEnvelope();
  record(unsafeField.error).fieldErrors = [
    {
      field: "expectedVersion\u0000",
      message: "Stale version.",
      reasonCode: "G6_STATE_INVALID",
    },
  ];

  const extendedFieldError = canonicalErrorEnvelope();
  record(extendedFieldError.error).fieldErrors = [
    {
      extra: true,
      field: "expectedVersion",
      message: "Stale version.",
      reasonCode: "G6_STATE_INVALID",
    },
  ];

  const gateReasonMismatch = canonicalErrorEnvelope();
  record(gateReasonMismatch.error).gateDecision = structuredClone(
    array(record(finalReviewBody().claimPacket).gateDecisions)[8],
  );

  const extendedGateDecision = canonicalErrorEnvelope();
  const gateDecision = structuredClone(
    array(record(finalReviewBody().claimPacket).gateDecisions)[8],
  );
  record(gateDecision).unexpected = true;
  record(extendedGateDecision.error).gateDecision = gateDecision;
  record(extendedGateDecision.error).reasonCodes = [];

  return [
    ["extra top-level key", extraTop],
    ["extra detail key", extraDetail],
    ["missing gateDecision", missingGateDecision],
    ["unsafe error code", unsafeCode],
    ["control character in message", unsafeMessage],
    ["overlong message", tooLongMessage],
    ["duplicate reason code", duplicateReasons],
    ["control character in field", unsafeField],
    ["extended field error", extendedFieldError],
    ["gate/envelope reason mismatch", gateReasonMismatch],
    ["extended gate decision", extendedGateDecision],
  ];
}

function legacyCaseView() {
  return {
    activeClarification: null,
    caseId: CASE_ID,
    claimPacket: null,
    createdAt: "2026-07-14T12:00:00Z",
    intakeSummary: null,
    portalState: "draft",
    redactedMetadata: {},
    state: "created",
    updatedAt: "2026-07-14T12:00:00Z",
    version: 1,
  };
}

function legacyFlowResponse() {
  return {
    case: legacyCaseView(),
    clarification: null,
    draftRevision: 1,
    gateHistory: [],
    phase: "review",
    portal: null,
    requestId: "legacy-request",
  };
}

function forgedCreatedSnapshot() {
  const body = createdBody();
  body.receipt = { humanApproved: true };
  return body;
}

type MutableRecord = Record<string, unknown>;

function record(value: unknown): MutableRecord {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Expected test record");
  }
  return value as MutableRecord;
}

function array(value: unknown): unknown[] {
  if (!Array.isArray(value)) throw new Error("Expected test array");
  return value;
}
