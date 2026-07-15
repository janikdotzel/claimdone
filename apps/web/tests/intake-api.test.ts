import { afterEach, describe, expect, it, vi } from "vitest";

import {
  answerClarification,
  claimDoneApiOrigin,
  claimDonePortalOrigin,
  createAndSubmitIntake,
  deleteAuthoritativeCase,
  deleteCase,
  deletePortalCase,
  submitIntake,
  type ClaimDoneFetch,
} from "../src/features/intake";

afterEach(() => {
  delete process.env.NEXT_PUBLIC_CLAIMDONE_API_ORIGIN;
  delete process.env.NEXT_PUBLIC_CLAIMDONE_PORTAL_ORIGIN;
});

describe("ClaimDone intake API boundary", () => {
  it("sends real files and the create-case version in the exact multipart shape", async () => {
    const images = [
      new File(["one"], "one.jpg", { type: "image/jpeg" }),
      new File(["two"], "two.png", { type: "image/png" }),
      new File(["three"], "three.jpg", { type: "image/jpeg" }),
    ];
    const fetcher = vi.fn<ClaimDoneFetch>(async (_input, init) => {
      expect(init?.method).toBe("POST");
      expect(init?.headers).toBeUndefined();
      expect(init?.body).toBeInstanceOf(FormData);
      const form = init?.body as FormData;
      expect(form.get("expectedVersion")).toBe("1");
      expect(form.getAll("images")).toEqual(images);
      expect(form.get("statementText")).toBe("Synthetic statement");
      expect(form.get("audio")).toBeNull();
      expect(form.getAll("exifDecisions")).toEqual(["strip", "retain", "strip"]);
      expect(form.get("sandboxAcknowledged")).toBe("true");
      expect(form.get("imageRightsConfirmed")).toBe("true");
      expect(form.get("dataProcessingApproved")).toBe("true");
      return Response.json(awaitingBody());
    });

    const response = await submitIntake(
      "case-api-001",
      {
        audio: null,
        dataProcessingApproved: true,
        exifDecisions: ["strip", "retain", "strip"],
        expectedVersion: 1,
        imageRightsConfirmed: true,
        images,
        sandboxAcknowledged: true,
        statementText: "Synthetic statement",
      },
      fetcher,
    );

    expect(response.phase).toBe("awaiting_clarification");
    expect(response.clarification.field).toBe("incident_time");
    expect(fetcher).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/cases/case-api-001/intake",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("enforces text XOR WAV and sends the WAV as the only statement part", async () => {
    const images = demoImages();
    const audio = new File(["RIFF----WAVE"], "statement.wav", { type: "audio/wav" });
    const fetcher: ClaimDoneFetch = async (_input, init) => {
      const form = init?.body as FormData;
      expect(form.get("audio")).toBe(audio);
      expect(form.get("statementText")).toBeNull();
      return Response.json(awaitingBody());
    };

    await submitIntake(
      "case-api-001",
      {
        audio,
        dataProcessingApproved: true,
        exifDecisions: ["strip", "strip", "strip"],
        expectedVersion: 1,
        imageRightsConfirmed: true,
        images,
        sandboxAcknowledged: true,
        statementText: null,
      },
      fetcher,
    );

    await expect(
      submitIntake(
        "case-api-001",
        {
          audio: null,
          dataProcessingApproved: true,
          exifDecisions: ["strip", "strip", "strip"],
          expectedVersion: 1,
          imageRightsConfirmed: true,
          images,
          sandboxAcknowledged: true,
          statementText: null,
        },
        fetcher,
      ),
    ).rejects.toMatchObject({ detail: { code: "CLIENT_INPUT_INVALID" } });
  });

  it("retains created-case cleanup ownership after a valid response", async () => {
    const onCaseCreated = vi.fn();
    const onCaseCleaned = vi.fn();
    const fetcher: ClaimDoneFetch = async (input) =>
      String(input).endsWith("/api/cases")
        ? Response.json(
            {
              ...caseBody("awaiting_clarification", "draft", 1),
              state: "created",
            },
            { status: 201 },
          )
        : Response.json(awaitingBody());

    const response = await createAndSubmitIntake(
      {
        audio: null,
        dataProcessingApproved: true,
        exifDecisions: ["strip", "strip", "strip"],
        imageRightsConfirmed: true,
        images: demoImages(),
        sandboxAcknowledged: true,
        statementText: "Synthetic statement",
      },
      fetcher,
      { onCaseCleaned, onCaseCreated },
    );

    expect(response.case.caseId).toBe("case-api-001");
    expect(onCaseCreated).toHaveBeenCalledOnce();
    expect(onCaseCreated).toHaveBeenCalledWith("case-api-001");
    expect(onCaseCleaned).not.toHaveBeenCalled();
  });

  it.each(["draft revision", "case binding", "gate order"])(
    "rejects an inconsistent %s response",
    async (variant) => {
      const body = awaitingBody();
      if (variant === "draft revision") body.draftRevision = 99;
      if (variant === "case binding") body.case.caseId = "case-other-001";
      if (variant === "gate order") body.gateHistory.reverse();
      const fetcher: ClaimDoneFetch = async () => Response.json(body);

      await expect(
        submitIntake(
          "case-api-001",
          {
            audio: null,
            dataProcessingApproved: true,
            exifDecisions: ["strip", "strip", "strip"],
            expectedVersion: 1,
            imageRightsConfirmed: true,
            images: demoImages(),
            sandboxAcknowledged: true,
            statementText: "Synthetic statement",
          },
          fetcher,
        ),
      ).rejects.toMatchObject({ detail: { code: "CLIENT_INVALID_RESPONSE" } });
    },
  );

  it.each([
    {
      label: "invented reason code",
      mutate: (body: ReturnType<typeof awaitingBody>) => {
        body.gateHistory[5]!.reasonCodes = ["G5_INVENTED_REASON"];
      },
    },
    {
      label: "reason code from the wrong gate",
      mutate: (body: ReturnType<typeof awaitingBody>) => {
        body.gateHistory[5]!.reasonCodes = ["G4_PROVENANCE_MISSING"];
      },
    },
    {
      label: "duplicate reason code",
      mutate: (body: ReturnType<typeof awaitingBody>) => {
        body.gateHistory[5]!.reasonCodes = [
          "G5_REQUIRED_FIELD_MISSING",
          "G5_REQUIRED_FIELD_MISSING",
        ];
      },
    },
    {
      label: "clarification limit combined with the allowed reason",
      mutate: (body: ReturnType<typeof awaitingBody>) => {
        body.gateHistory[5]!.reasonCodes = [
          "G5_REQUIRED_FIELD_MISSING",
          "G5_CLARIFICATION_LIMIT",
        ];
      },
    },
    {
      label: "invalid question combined with the allowed reason",
      mutate: (body: ReturnType<typeof awaitingBody>) => {
        body.gateHistory[5]!.reasonCodes = [
          "G5_REQUIRED_FIELD_MISSING",
          "G5_QUESTION_INVALID",
        ];
      },
    },
    {
      label: "passing gate with a reason",
      mutate: (body: ReturnType<typeof awaitingBody>) => {
        body.gateHistory[0]!.reasonCodes = ["G0_CONSENT_MISSING"];
      },
    },
    {
      label: "contradictory pass flags",
      mutate: (body: ReturnType<typeof awaitingBody>) => {
        body.gateHistory[0]!.deterministicPassed = false;
      },
    },
    {
      label: "model block on a non-model gate",
      mutate: (body: ReturnType<typeof awaitingBody>) => {
        body.gateHistory[2]!.deterministicPassed = false;
        body.gateHistory[2]!.modelBlocked = true;
        body.gateHistory[2]!.passed = false;
        body.gateHistory[2]!.reasonCodes = ["G2_SCHEMA_INVALID"];
      },
    },
  ])("rejects GateDecision payload with $label", async ({ mutate }) => {
    const body = awaitingBody();
    mutate(body);
    const fetcher: ClaimDoneFetch = async () => Response.json(body);

    await expect(
      submitIntake(
        "case-api-001",
        {
          audio: null,
          dataProcessingApproved: true,
          exifDecisions: ["strip", "strip", "strip"],
          expectedVersion: 1,
          imageRightsConfirmed: true,
          images: demoImages(),
          sandboxAcknowledged: true,
          statementText: "Synthetic statement",
        },
        fetcher,
      ),
    ).rejects.toMatchObject({ detail: { code: "CLIENT_INVALID_RESPONSE" } });
  });

  it("uses only the two approved INT-001 loopback origins", () => {
    expect(claimDoneApiOrigin()).toBe("http://127.0.0.1:8000");
    expect(claimDonePortalOrigin()).toBe("http://127.0.0.1:3000");
  });

  it.each([
    ["api", "http://localhost:8000"],
    ["api", "http://127.0.0.1.evil.example:8000"],
    ["api", "http://2130706433:8000"],
    ["api", "http://127.0.0.1:8000/"],
    ["api", " http://127.0.0.1:8000"],
    ["api", "https://127.0.0.1:8000"],
    ["api", "http://user@127.0.0.1:8000"],
    ["api", "http://127.0.0.1:8000/api"],
    ["api", "http://127.0.0.1:8000?debug=true"],
    ["portal", "http://localhost:3000"],
    ["portal", "http://127.0.0.1:3001"],
    ["portal", "http://127.0.0.1:3000/#review"],
    ["portal", "https://127.0.0.1:3000"],
  ])("rejects non-approved %s origin %s", (kind, origin) => {
    if (kind === "api") {
      process.env.NEXT_PUBLIC_CLAIMDONE_API_ORIGIN = origin;
      expect(() => claimDoneApiOrigin()).toThrow("approved loopback origin");
    } else {
      process.env.NEXT_PUBLIC_CLAIMDONE_PORTAL_ORIGIN = origin;
      expect(() => claimDonePortalOrigin()).toThrow("approved loopback origin");
    }
  });

  it("posts exactly one version-bound clarification answer and accepts review", async () => {
    const fetcher = vi.fn<ClaimDoneFetch>(async (_input, init) => {
      expect(JSON.parse(String(init?.body))).toEqual({ answer: "14:30", expectedVersion: 4 });
      return Response.json(reviewBody());
    });

    const response = await answerClarification(
      "case-api-001",
      "clarification-001",
      4,
      "14:30",
      fetcher,
    );

    expect(response.phase).toBe("review");
    expect(response.case.state).toBe("verifying");
    expect(response.case.portalState).toBe("review");
    expect(response.portal.verificationState).toBe("pending");
    expect(response.gateHistory.map(({ gateId }) => gateId)).toEqual([
      "G0",
      "G1",
      "G2",
      "G3",
      "G4",
      "G5",
    ]);
  });

  it("preserves ErrorEnvelope field errors, reasons, and currentVersion", async () => {
    const fetcher: ClaimDoneFetch = async () =>
      Response.json(
        {
          error: {
            code: "CASE_VERSION_CONFLICT",
            currentVersion: 5,
            fieldErrors: [
              {
                field: "expectedVersion",
                message: "Use the current version.",
                reasonCode: null,
              },
            ],
            gateDecision: null,
            message: "The case changed since it was loaded.",
            reasonCodes: ["G6_STATE_INVALID"],
          },
        },
        { status: 409 },
      );

    await expect(
      answerClarification(
        "case-api-001",
        "clarification-001",
        4,
        "14:30",
        fetcher,
      ),
    ).rejects.toMatchObject({
      detail: {
        code: "CASE_VERSION_CONFLICT",
        currentVersion: 5,
        fieldErrors: [
          { field: "expectedVersion", message: "Use the current version." },
        ],
        reasonCodes: ["G6_STATE_INVALID"],
      },
      status: 409,
    });
  });

  it.each([
    "https://attacker.invalid/sandbox/A/cases/case-api-001",
    "http://127.0.0.1:3000/sandbox/B/cases/case-api-001",
    "http://127.0.0.1:3000/sandbox/A/cases/case-other-001",
    "http://127.0.0.1:3000/sandbox/A/cases/case-api-001?next=evil",
  ])("rejects unsafe portal review URL %s", async (reviewUrl) => {
    const body = reviewBody();
    body.portal.reviewUrl = reviewUrl;
    const fetcher: ClaimDoneFetch = async () => Response.json(body);

    await expect(
      answerClarification(
        "case-api-001",
        "clarification-001",
        4,
        "14:30",
        fetcher,
      ),
    ).rejects.toMatchObject({
      detail: { code: "CLIENT_INVALID_RESPONSE" },
    });
  });

  it("rejects a second clarification round", async () => {
    const body = awaitingBody();
    body.clarification.round = 2;
    const fetcher: ClaimDoneFetch = async () => Response.json(body);
    const images = [
      new File(["1"], "1.jpg", { type: "image/jpeg" }),
      new File(["2"], "2.jpg", { type: "image/jpeg" }),
      new File(["3"], "3.jpg", { type: "image/jpeg" }),
    ];

    await expect(
      submitIntake(
        "case-api-001",
        {
          audio: null,
          dataProcessingApproved: true,
          exifDecisions: ["strip", "strip", "strip"],
          expectedVersion: 1,
          imageRightsConfirmed: true,
          images,
          sandboxAcknowledged: true,
          statementText: "Synthetic statement",
        },
        fetcher,
      ),
    ).rejects.toMatchObject({ detail: { code: "CLIENT_INVALID_RESPONSE" } });
  });

  it("deletes the authoritative server case before local start-over can complete", async () => {
    const fetcher = vi.fn<ClaimDoneFetch>(async () => new Response(null, { status: 204 }));

    await deleteCase("case-api-001", fetcher);

    expect(fetcher).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/cases/case-api-001",
      { cache: "no-store", method: "DELETE" },
    );
  });

  it("deletes backend and portal resources before authoritative start-over", async () => {
    const requested: string[] = [];
    const fetcher: ClaimDoneFetch = async (input, init) => {
      requested.push(String(input));
      expect(init).toEqual({ cache: "no-store", method: "DELETE" });
      return new Response(null, { status: 204 });
    };

    await deleteAuthoritativeCase("case-api-001", fetcher);

    expect(requested).toEqual([
      "http://127.0.0.1:8000/api/cases/case-api-001",
      "http://127.0.0.1:3000/api/sandbox/cases/case-api-001",
    ]);
  });

  it("cleans both resources when create succeeds but intake rejects", async () => {
    const requested: string[] = [];
    const fetcher: ClaimDoneFetch = async (input, init) => {
      const url = String(input);
      requested.push(`${init?.method ?? "GET"} ${url}`);
      if (url.endsWith("/api/cases") && init?.method === "POST") {
        return Response.json(
          {
            ...caseBody("awaiting_clarification", "draft", 1),
            state: "created",
          },
          { status: 201 },
        );
      }
      if (url.endsWith("/intake")) {
        return Response.json(
          {
            error: {
              code: "GATE_FAILED",
              currentVersion: 2,
              fieldErrors: [],
              gateDecision: null,
              message: "Authoritative G0 rejected the intake.",
              reasonCodes: ["G0_IMAGE_TYPE_INVALID"],
            },
          },
          { status: 422 },
        );
      }
      return new Response(null, { status: 204 });
    };

    await expect(
      createAndSubmitIntake(
        {
          audio: null,
          dataProcessingApproved: true,
          exifDecisions: ["strip", "strip", "strip"],
          imageRightsConfirmed: true,
          images: demoImages(),
          sandboxAcknowledged: true,
          statementText: "Synthetic statement",
        },
        fetcher,
      ),
    ).rejects.toMatchObject({ detail: { code: "GATE_FAILED" } });

    expect(requested).toContain(
      "DELETE http://127.0.0.1:8000/api/cases/case-api-001",
    );
    expect(requested).toContain(
      "DELETE http://127.0.0.1:3000/api/sandbox/cases/case-api-001",
    );
  });

  it("supports idempotent portal cleanup independently after partial cleanup", async () => {
    const fetcher = vi.fn<ClaimDoneFetch>(async () => new Response(null, { status: 204 }));

    await deletePortalCase("case-api-001", fetcher);
    await deletePortalCase("case-api-001", fetcher);

    expect(fetcher).toHaveBeenCalledTimes(2);
  });
});

function caseBody(
  state: "awaiting_clarification" | "verifying",
  portalState: "draft" | "review",
  version: number,
) {
  return {
    activeClarification: null,
    caseId: "case-api-001",
    claimPacket: null,
    createdAt: "2026-07-14T12:00:00Z",
    intakeSummary: null,
    portalState,
    redactedMetadata: {},
    state,
    updatedAt: "2026-07-14T12:00:01Z",
    version,
  };
}

function demoImages(): File[] {
  return [
    new File(["one"], "one.jpg", { type: "image/jpeg" }),
    new File(["two"], "two.png", { type: "image/png" }),
    new File(["three"], "three.jpg", { type: "image/jpeg" }),
  ];
}

function gateHistory(g5Passed: boolean) {
  return (["G0", "G1", "G2", "G3", "G4", "G5"] as const).map((gateId) => ({
    contractVersion: "4.0.0",
    decidedAt: "2026-07-14T12:00:01Z",
    deterministicPassed: gateId === "G5" ? g5Passed : true,
    evidenceRefs: [],
    gateId,
    modelBlocked: false,
    passed: gateId === "G5" ? g5Passed : true,
    reasonCodes:
      gateId === "G5" && !g5Passed ? ["G5_REQUIRED_FIELD_MISSING"] : [],
  }));
}

function awaitingBody() {
  return {
    case: caseBody("awaiting_clarification", "draft", 4),
    clarification: {
      clarificationId: "clarification-001",
      expectedVersion: 4,
      field: "incident_time",
      question: "What time did the staged incident happen?",
      round: 1,
    },
    draftRevision: 4,
    gateHistory: gateHistory(false),
    phase: "awaiting_clarification",
    portal: null,
    requestId: "request-intake-001",
  };
}

function reviewBody() {
  return {
    case: caseBody("verifying", "review", 8),
    clarification: null,
    draftRevision: 8,
    gateHistory: gateHistory(true),
    phase: "review",
    portal: {
      renderedValues: {
        attachments: [
          "model-0123456789abcdef0123456789abcdef.jpg",
          "model-fedcba9876543210fedcba9876543210.png",
          "model-00000000000000000000000000000000.jpg",
        ],
        incidentTime: "14:30:00",
      },
      reviewUrl: "http://127.0.0.1:3000/sandbox/A/cases/case-api-001",
      verificationState: "pending",
    },
    requestId: "request-answer-001",
  };
}
