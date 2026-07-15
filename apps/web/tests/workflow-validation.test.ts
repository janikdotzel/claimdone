import { describe, expect, it } from "vitest";

import {
  BLOCKED_SNAPSHOT,
  CLARIFICATION_SNAPSHOT,
  G8_BLOCKED_SNAPSHOT,
  QUOTA_EVENT,
  READY_SNAPSHOT,
  REPAIR_SNAPSHOT,
  REVIEW_SNAPSHOT,
  SHOWCASE_EVENTS,
  VERIFYING_SNAPSHOT,
} from "../src/features/workflow/fixtures";
import {
  parseWorkflowEventEnvelope,
  parseWorkflowSnapshot,
  WorkflowPayloadError,
} from "../src/features/workflow/validation";

describe("closed workflow snapshot parser", () => {
  it("accepts canonical Contract 4.0 verifying and review fixtures", () => {
    expect(parseWorkflowSnapshot(VERIFYING_SNAPSHOT).case.state).toBe("verifying");
    expect(parseWorkflowSnapshot(REVIEW_SNAPSHOT).case.state).toBe("review");
    expect(parseWorkflowSnapshot(REPAIR_SNAPSHOT).verificationAttempts?.attempts).toHaveLength(2);
    expect(parseWorkflowSnapshot(G8_BLOCKED_SNAPSHOT).case.state).toBe("blocked");
  });

  it("rejects unknown fields, boolean versions, and malformed calendar values", () => {
    const unknown = cloneRecord(REVIEW_SNAPSHOT);
    unknown.rawProviderResponse = "unsafe";
    expect(() => parseWorkflowSnapshot(unknown)).toThrow(WorkflowPayloadError);

    const boolVersion = cloneRecord(REVIEW_SNAPSHOT);
    record(boolVersion.case).version = true;
    expect(() => parseWorkflowSnapshot(boolVersion)).toThrow(/integer/);

    const badTimestamp = cloneRecord(REVIEW_SNAPSHOT);
    record(badTimestamp.case).updatedAt = "2026-02-30T24:00:00Z";
    expect(() => parseWorkflowSnapshot(badTimestamp)).toThrow(/calendar|clock/);
  });

  it("rejects identifier-like storage handles with slash or whitespace", () => {
    for (const value of ["local/ref", " local-ref-1"] as const) {
      const unsafe = cloneRecord(REVIEW_SNAPSHOT);
      const packet = record(unsafe.claimPacket);
      record(array(packet.evidence)[0]).localRef = value;
      expect(() => parseWorkflowSnapshot(unsafe)).toThrow(/identifier/);
    }
  });

  it("fails closed on impossible state/payload combinations", () => {
    const createdWithPortal = cloneRecord(REVIEW_SNAPSHOT);
    record(createdWithPortal.case).state = "created";
    expect(() => parseWorkflowSnapshot(createdWithPortal)).toThrow(/ClaimPacket|created/);

    const reviewWithoutAttempts = cloneRecord(REVIEW_SNAPSHOT);
    reviewWithoutAttempts.verificationAttempts = null;
    expect(() => parseWorkflowSnapshot(reviewWithoutAttempts)).toThrow(/completed verification/);

    const verifyingDraft = cloneRecord(VERIFYING_SNAPSHOT);
    record(verifyingDraft.portalSession).state = "draft";
    expect(() => parseWorkflowSnapshot(verifyingDraft)).toThrow(/invalid for verifying/);

    const terminalApprovalClaim = cloneRecord(G8_BLOCKED_SNAPSHOT);
    record(terminalApprovalClaim.claimPacket).portalState = "human_approved";
    expect(() => parseWorkflowSnapshot(terminalApprovalClaim)).toThrow(
      /terminal stop states|match ClaimPacket/,
    );
  });

  it("binds the active clarification to case time and first deterministic blocker", () => {
    const wrongField = cloneRecord(CLARIFICATION_SNAPSHOT);
    record(wrongField.clarification).field = "location";
    expect(() => parseWorkflowSnapshot(wrongField)).toThrow(
      /first deterministic missing or conflicting required field/,
    );

    const staleTime = cloneRecord(CLARIFICATION_SNAPSHOT);
    record(staleTime.clarification).requestedAt = "2026-07-14T11:59:59Z";
    expect(() => parseWorkflowSnapshot(staleTime)).toThrow(/case lifetime/);
  });

  it("rejects fabricated gate authority in both pass and fail directions", () => {
    const fakePass = cloneRecord(REVIEW_SNAPSHOT);
    const gates = array(record(fakePass.claimPacket).gateDecisions);
    const g8 = record(gates[8]);
    g8.reasonCodes = ["G8_FIELD_MISMATCH"];
    g8.deterministicPassed = true;
    g8.passed = true;
    expect(() => parseWorkflowSnapshot(fakePass)).toThrow(/deterministicPassed/);

    const fakeFail = cloneRecord(REVIEW_SNAPSHOT);
    const g4 = record(array(record(fakeFail.claimPacket).gateDecisions)[4]);
    g4.deterministicPassed = false;
    g4.passed = false;
    g4.reasonCodes = [];
    expect(() => parseWorkflowSnapshot(fakeFail)).toThrow(
      /reasonCodes|deterministicPassed/,
    );
  });

  it("enforces the exact state-bound visible plan without authority escalation", () => {
    const clarificationWithFill = cloneRecord(CLARIFICATION_SNAPSHOT);
    const clarificationSteps = array(
      record(record(clarificationWithFill.claimPacket).plan).steps,
    );
    record(clarificationSteps[2]).tool = "inspect_form";
    expect(() => parseWorkflowSnapshot(clarificationWithFill)).toThrow(
      /exact tool sequence/,
    );

    const readyWithQuestion = cloneRecord(READY_SNAPSHOT);
    const readySteps = array(record(record(readyWithQuestion.claimPacket).plan).steps);
    record(readySteps[2]).tool = "ask_clarification";
    expect(() => parseWorkflowSnapshot(readyWithQuestion)).toThrow(
      /exact tool sequence/,
    );
  });

  it("selects a terminal plan from the completed G0..G5 phase, not the last gate index", () => {
    for (const terminalState of ["failed", "abandoned"] as const) {
      const afterAnalysis = cloneRecord(READY_SNAPSHOT);
      record(afterAnalysis.case).state = terminalState;
      record(afterAnalysis.claimPacket).state = terminalState;
      expect(parseWorkflowSnapshot(afterAnalysis).case.state).toBe(terminalState);

      const regressedPlan = cloneRecord(afterAnalysis);
      record(record(regressedPlan.claimPacket).plan).steps = structuredClone(
        array(record(record(BLOCKED_SNAPSHOT.claimPacket).plan).steps),
      );
      expect(() => parseWorkflowSnapshot(regressedPlan)).toThrow(/exact tool sequence/);
    }

    const laterGateFailure = cloneRecord(READY_SNAPSHOT);
    record(laterGateFailure.case).state = "failed";
    const laterPacket = record(laterGateFailure.claimPacket);
    laterPacket.state = "failed";
    array(laterPacket.gateDecisions).push({
      contractVersion: "4.0.0",
      decidedAt: "2026-07-14T12:00:06Z",
      deterministicPassed: false,
      evidenceRefs: [],
      gateId: "G6",
      modelBlocked: false,
      passed: false,
      reasonCodes: ["G6_STATE_INVALID"],
    });
    expect(parseWorkflowSnapshot(laterGateFailure).case.state).toBe("failed");

    const earlyFailureWithFillPlan = cloneRecord(BLOCKED_SNAPSHOT);
    record(record(earlyFailureWithFillPlan.claimPacket).plan).steps = structuredClone(
      array(record(record(READY_SNAPSHOT.claimPacket).plan).steps),
    );
    expect(() => parseWorkflowSnapshot(earlyFailureWithFillPlan)).toThrow(
      /exact tool sequence/,
    );
  });

  it("accepts only the canonical G4 conflict diagnostic before G5 clarification", () => {
    const conflictClarification = cloneRecord(CLARIFICATION_SNAPSHOT);
    const conflictFacts = array(record(conflictClarification.claimPacket).facts);
    conflictFacts.push({
      confidence: null,
      factId: "fact-date-conflict",
      field: "incident_date",
      sourceRefs: ["prov-statement"],
      status: "user_stated",
      value: "2026-07-13",
    });
    const conflictGates = array(record(conflictClarification.claimPacket).gateDecisions);
    Object.assign(record(conflictGates[4]), {
      deterministicPassed: false,
      modelBlocked: false,
      passed: false,
      reasonCodes: ["G4_CONFLICTING_SOURCES"],
    });
    expect(parseWorkflowSnapshot(conflictClarification).case.state).toBe(
      "awaiting_clarification",
    );

    const lowConfidenceClarification = cloneRecord(CLARIFICATION_SNAPSHOT);
    const lowConfidenceGates = array(
      record(lowConfidenceClarification.claimPacket).gateDecisions,
    );
    const lowConfidenceFact = array(
      record(lowConfidenceClarification.claimPacket).facts,
    ).find((fact) => record(fact).status === "observed");
    record(lowConfidenceFact).confidence = 0.79;
    Object.assign(record(lowConfidenceGates[4]), {
      deterministicPassed: false,
      modelBlocked: false,
      passed: false,
      reasonCodes: ["G4_CONFIDENCE_BELOW_THRESHOLD"],
    });
    expect(() => parseWorkflowSnapshot(lowConfidenceClarification)).toThrow(
      /only G4_CONFLICTING_SOURCES/,
    );

    const unrelatedConflict = cloneRecord(CLARIFICATION_SNAPSHOT);
    array(record(unrelatedConflict.claimPacket).facts).push({
      confidence: null,
      factId: "fact-collision-conflict",
      field: "collision_type",
      sourceRefs: ["prov-statement"],
      status: "user_stated",
      value: "side_impact",
    });
    setG4Conflict(unrelatedConflict);
    expect(() => parseWorkflowSnapshot(unrelatedConflict)).toThrow(
      /conflicting required claim field/,
    );
  });

  it("orders clarification targets over the union of missing and required conflicts", () => {
    const missingBeforeConflict = cloneRecord(CLARIFICATION_SNAPSHOT);
    const facts = array(record(missingBeforeConflict.claimPacket).facts);
    facts.push(
      {
        confidence: null,
        factId: "fact-location-a",
        field: "location",
        sourceRefs: ["prov-statement"],
        status: "user_stated",
        value: "Staged location A",
      },
      {
        confidence: null,
        factId: "fact-location-b",
        field: "location",
        sourceRefs: ["prov-statement"],
        status: "user_stated",
        value: "Staged location B",
      },
    );
    setG4Conflict(missingBeforeConflict);
    expect(parseWorkflowSnapshot(missingBeforeConflict).case.state).toBe(
      "awaiting_clarification",
    );

    const skipsFirstBlocker = cloneRecord(missingBeforeConflict);
    record(skipsFirstBlocker.clarification).field = "location";
    expect(() => parseWorkflowSnapshot(skipsFirstBlocker)).toThrow(
      /first deterministic missing or conflicting required field/,
    );

    const conflictOnly = cloneRecord(CLARIFICATION_SNAPSHOT);
    const conflictOnlyPacket = record(conflictOnly.claimPacket);
    const conflictOnlyClaim = record(conflictOnlyPacket.claim);
    const completeClaim = record(record(READY_SNAPSHOT.claimPacket).claim);
    conflictOnlyClaim.incidentDate = completeClaim.incidentDate;
    conflictOnlyClaim.missingRequiredFields = [];
    conflictOnlyClaim.fieldProvenance = structuredClone(completeClaim.fieldProvenance);
    array(conflictOnlyPacket.facts).push({
      confidence: null,
      factId: "fact-date-conflict-only",
      field: "incident_date",
      sourceRefs: ["prov-statement"],
      status: "user_stated",
      value: "2026-07-13",
    });
    setG4Conflict(conflictOnly);
    expect(parseWorkflowSnapshot(conflictOnly).case.state).toBe(
      "awaiting_clarification",
    );
  });

  it("keeps the deterministic observed-confidence boundary at 0.8", () => {
    const belowThreshold = cloneRecord(REVIEW_SNAPSHOT);
    const observedFact = array(record(belowThreshold.claimPacket).facts).find(
      (fact) => record(fact).status === "observed",
    );
    record(observedFact).confidence = 0.79;
    expect(() => parseWorkflowSnapshot(belowThreshold)).toThrow(
      /deterministic 0\.8 fact threshold/,
    );

    const atThreshold = cloneRecord(REVIEW_SNAPSHOT);
    const boundaryFact = array(record(atThreshold.claimPacket).facts).find(
      (fact) => record(fact).status === "observed",
    );
    record(boundaryFact).confidence = 0.8;
    expect(parseWorkflowSnapshot(atThreshold).case.state).toBe("review");
  });

  it("rejects repair metadata that is not bound to the sole mismatch", () => {
    const wrongSource = cloneRecord(REPAIR_SNAPSHOT);
    const attempts = array(record(wrongSource.verificationAttempts).attempts);
    record(record(attempts[0]).repair).sourceRefs = ["prov-image-2"];
    expect(() => parseWorkflowSnapshot(wrongSource)).toThrow(
      /sole mismatch|provenance/,
    );

    const changedNonTarget = cloneRecord(REPAIR_SNAPSHOT);
    const changedAttempts = array(record(changedNonTarget.verificationAttempts).attempts);
    const secondFields = array(record(record(changedAttempts[1]).report).fieldResults);
    const incidentTime = secondFields.find(
      (field) => record(field).field === "incident_time",
    );
    record(incidentTime).actual = "14:31:00";
    record(incidentTime).status = "mismatch";
    expect(() => parseWorkflowSnapshot(changedNonTarget)).toThrow(
      /deterministicMatch|non-target rendered field/,
    );

    const changedAttachmentIdentity = cloneRecord(REPAIR_SNAPSHOT);
    const identityAttempts = array(
      record(changedAttachmentIdentity.verificationAttempts).attempts,
    );
    const repairedReport = record(record(identityAttempts[1]).report);
    const replacement = ["alternate-ref-1", "alternate-ref-2", "alternate-ref-3"];
    repairedReport.expectedAttachmentIds = replacement;
    repairedReport.actualAttachmentIds = replacement;
    expect(() => parseWorkflowSnapshot(changedAttachmentIdentity)).toThrow(
      /scalar repair cannot change attachment verification/,
    );

    const attachmentMismatchRepair = cloneRecord(REPAIR_SNAPSHOT);
    const mismatchAttempts = array(
      record(attachmentMismatchRepair.verificationAttempts).attempts,
    );
    record(record(mismatchAttempts[0]).report).actualAttachmentIds = [
      "wrong-ref-1",
      "wrong-ref-2",
      "wrong-ref-3",
    ];
    expect(() => parseWorkflowSnapshot(attachmentMismatchRepair)).toThrow(
      /complete deterministic scalar mismatch/,
    );
  });

  it("derives verification field and report authority instead of trusting flags", () => {
    const falseMatch = cloneRecord(REVIEW_SNAPSHOT);
    const verification = record(record(falseMatch.claimPacket).verification);
    const location = record(
      array(verification.fieldResults).find(
        (entry) => record(entry).field === "location",
      ),
    );
    location.actual = "Different staged value";
    location.status = "match";
    expect(() => parseWorkflowSnapshot(falseMatch)).toThrow(/match requires equal/);

    const falseApproval = cloneRecord(REVIEW_SNAPSHOT);
    record(record(falseApproval.claimPacket).verification).reviewAllowed = false;
    expect(() => parseWorkflowSnapshot(falseApproval)).toThrow(/reviewAllowed/);
  });

  it("makes ordered attachment identity authoritative over counts and model agreement", () => {
    for (const actualIds of [
      ["wrong-ref-1", "wrong-ref-2", "wrong-ref-3"],
      ["local-ref-2", "local-ref-1", "local-ref-3"],
    ]) {
      const forgedMatch = cloneRecord(REVIEW_SNAPSHOT);
      record(record(forgedMatch.claimPacket).verification).actualAttachmentIds = actualIds;
      expect(() => parseWorkflowSnapshot(forgedMatch)).toThrow(
        /derived from all fields and attachments/,
      );
    }

    const wrongExpected = cloneRecord(REVIEW_SNAPSHOT);
    const expectedReport = record(record(wrongExpected.claimPacket).verification);
    const replacement = ["alternate-ref-1", "alternate-ref-2", "alternate-ref-3"];
    expectedReport.expectedAttachmentIds = replacement;
    expectedReport.actualAttachmentIds = replacement;
    expect(() => parseWorkflowSnapshot(wrongExpected)).toThrow(
      /canonical ClaimData attachments/,
    );

    const inconsistentCount = cloneRecord(REVIEW_SNAPSHOT);
    record(record(inconsistentCount.claimPacket).verification).actualAttachmentCount = 2;
    expect(() => parseWorkflowSnapshot(inconsistentCount)).toThrow(
      /actualAttachmentIds length/,
    );
  });

  it("rejects padded IDs on every attachment identity surface", () => {
    const paddedEvidence = cloneRecord(REVIEW_SNAPSHOT);
    record(array(record(paddedEvidence.claimPacket).evidence)[0]).localRef =
      " local-ref-1";
    expect(() => parseWorkflowSnapshot(paddedEvidence)).toThrow(/identifier/);

    const paddedClaim = cloneRecord(REVIEW_SNAPSHOT);
    array(record(record(paddedClaim.claimPacket).claim).attachments)[0] =
      " local-ref-1";
    expect(() => parseWorkflowSnapshot(paddedClaim)).toThrow(/identifier/);

    const paddedExpected = cloneRecord(REVIEW_SNAPSHOT);
    array(
      record(record(paddedExpected.claimPacket).verification).expectedAttachmentIds,
    )[0] = " local-ref-1";
    expect(() => parseWorkflowSnapshot(paddedExpected)).toThrow(/identifier/);

    const paddedActual = cloneRecord(REVIEW_SNAPSHOT);
    array(
      record(record(paddedActual.claimPacket).verification).actualAttachmentIds,
    )[0] = " local-ref-1";
    expect(() => parseWorkflowSnapshot(paddedActual)).toThrow(/identifier/);

    const paddedPortal = cloneRecord(REVIEW_SNAPSHOT);
    array(record(record(paddedPortal.portalSession).fields).attachments)[0] =
      " local-ref-1";
    expect(() => parseWorkflowSnapshot(paddedPortal)).toThrow(/identifier/);
  });

  it("rejects duplicate attachment identities before equality can launder them", () => {
    const duplicateClaim = cloneRecord(REVIEW_SNAPSHOT);
    const claimAttachments = array(
      record(record(duplicateClaim.claimPacket).claim).attachments,
    );
    claimAttachments[1] = claimAttachments[0];
    expect(() => parseWorkflowSnapshot(duplicateClaim)).toThrow(/must be unique/);

    const duplicateExpected = cloneRecord(REVIEW_SNAPSHOT);
    const expectedIds = array(
      record(record(duplicateExpected.claimPacket).verification).expectedAttachmentIds,
    );
    expectedIds[1] = expectedIds[0];
    expect(() => parseWorkflowSnapshot(duplicateExpected)).toThrow(/must be unique/);

    const duplicateActual = cloneRecord(REVIEW_SNAPSHOT);
    const actualIds = array(
      record(record(duplicateActual.claimPacket).verification).actualAttachmentIds,
    );
    actualIds[1] = actualIds[0];
    expect(() => parseWorkflowSnapshot(duplicateActual)).toThrow(/must be unique/);

    const duplicatePortal = cloneRecord(REVIEW_SNAPSHOT);
    const portalAttachments = array(
      record(record(duplicatePortal.portalSession).fields).attachments,
    );
    portalAttachments[1] = portalAttachments[0];
    expect(() => parseWorkflowSnapshot(duplicatePortal)).toThrow(/must be unique/);

    const duplicateEvidenceProjection = cloneRecord(REVIEW_SNAPSHOT);
    const images = array(record(duplicateEvidenceProjection.claimPacket).evidence)
      .map((entry) => record(entry))
      .filter((entry) => entry.kind === "image");
    record(images[1]).localRef = record(images[0]).localRef;
    expect(() => parseWorkflowSnapshot(duplicateEvidenceProjection)).toThrow(
      /image localRefs/,
    );
  });

  it("binds every attempt attachment identity to the packet and rendered portal", () => {
    const wrongExpected = cloneRecord(VERIFYING_SNAPSHOT);
    wrongExpected.verificationAttempts = structuredClone(
      REVIEW_SNAPSHOT.verificationAttempts,
    );
    const expectedAttempt = record(
      array(record(wrongExpected.verificationAttempts).attempts)[0],
    );
    const expectedReport = record(expectedAttempt.report);
    const replacement = ["alternate-ref-1", "alternate-ref-2", "alternate-ref-3"];
    expectedReport.expectedAttachmentIds = replacement;
    expectedReport.actualAttachmentIds = replacement;
    record(record(wrongExpected.portalSession).fields).attachments = replacement;
    expect(() => parseWorkflowSnapshot(wrongExpected)).toThrow(
      /canonical ClaimData attachments/,
    );

    const wrongRendered = cloneRecord(VERIFYING_SNAPSHOT);
    wrongRendered.verificationAttempts = structuredClone(
      REVIEW_SNAPSHOT.verificationAttempts,
    );
    record(record(wrongRendered.portalSession).fields).attachments = [
      "local-ref-2",
      "local-ref-1",
      "local-ref-3",
    ];
    expect(() => parseWorkflowSnapshot(wrongRendered)).toThrow(
      /rendered portal attachments/,
    );
  });

  it("treats short non-null attachment IDs as mismatch rather than missing", () => {
    const blocked = cloneRecord(G8_BLOCKED_SNAPSHOT);
    const packet = record(blocked.claimPacket);
    const packetReport = record(packet.verification);
    const attempts = array(record(blocked.verificationAttempts).attempts);
    const attempt = record(attempts[0]);
    const attemptReport = record(attempt.report);
    const shortIds = ["local-ref-1", "local-ref-2"];
    for (const report of [packetReport, attemptReport]) {
      report.actualAttachmentCount = 2;
      report.actualAttachmentIds = shortIds;
    }
    for (const gate of [
      record(array(packet.gateDecisions)[8]),
      record(attempt.gateDecision),
    ]) {
      gate.reasonCodes = ["G8_FIELD_MISMATCH", "G8_ATTACHMENT_MISMATCH"];
    }
    record(record(blocked.portalSession).fields).attachments = shortIds;

    expect(parseWorkflowSnapshot(blocked).case.state).toBe("blocked");
  });

  it("derives compound G8 reasons in backend order", () => {
    const compound = cloneRecord(G8_BLOCKED_SNAPSHOT);
    setCompoundG8Failure(compound, [
      "G8_FIELD_MISMATCH",
      "G8_ATTACHMENT_MISMATCH",
      "G8_REQUIRED_FIELD_MISSING",
      "G8_MODEL_MISMATCH",
    ]);
    expect(parseWorkflowSnapshot(compound).case.state).toBe("blocked");

    const wrongOrder = cloneRecord(compound);
    const wrongReasons = [
      "G8_FIELD_MISMATCH",
      "G8_REQUIRED_FIELD_MISSING",
      "G8_ATTACHMENT_MISMATCH",
      "G8_MODEL_MISMATCH",
    ];
    const attempts = array(record(wrongOrder.verificationAttempts).attempts);
    record(record(attempts[0]).gateDecision).reasonCodes = wrongReasons;
    expect(() => parseWorkflowSnapshot(wrongOrder)).toThrow(
      /derived exactly from the report/,
    );
  });

  it("accepts PortalDraftFields in a review-state session while preserving case review authority", () => {
    for (const attachmentCount of [0, 1, 2, 3]) {
      const verifying = cloneRecord(VERIFYING_SNAPSHOT);
      const fields = record(record(verifying.portalSession).fields);
      Object.assign(fields, {
        attachments: ["draft-ref-1", "draft-ref-2", "draft-ref-3"].slice(
          0,
          attachmentCount,
        ),
        claimantName: "",
        counterpartyKnown: "",
        incidentDate: "",
        incidentTime: "",
        location: "",
        narrative: "",
        policyReference: "",
        vehicleRegistration: "",
      });
      expect(parseWorkflowSnapshot(verifying).portalSession?.state).toBe("review");
    }

    const tooManyAttachments = cloneRecord(VERIFYING_SNAPSHOT);
    record(record(tooManyAttachments.portalSession).fields).attachments = [
      "draft-ref-1",
      "draft-ref-2",
      "draft-ref-3",
      "draft-ref-4",
    ];
    expect(() => parseWorkflowSnapshot(tooManyAttachments)).toThrow(/0\.\.3 items/);

    const incompleteCaseReview = cloneRecord(REVIEW_SNAPSHOT);
    record(record(incompleteCaseReview.portalSession).fields).location = "";
    expect(() => parseWorkflowSnapshot(incompleteCaseReview)).toThrow(
      /exactly equal the canonical claim values/,
    );
  });

  it("rejects broken evidence, provenance, and attachment relationships", () => {
    const duplicateEvidence = cloneRecord(REVIEW_SNAPSHOT);
    const evidence = array(record(duplicateEvidence.claimPacket).evidence);
    record(evidence[1]).evidenceId = record(evidence[0]).evidenceId;
    expect(() => parseWorkflowSnapshot(duplicateEvidence)).toThrow(/Evidence IDs|evidence IDs/);

    const unknownSource = cloneRecord(REVIEW_SNAPSHOT);
    const facts = array(record(unknownSource.claimPacket).facts);
    record(facts[0]).sourceRefs = ["prov-missing"];
    expect(() => parseWorkflowSnapshot(unknownSource)).toThrow(/absent from provenance/);

    const attachmentSwap = cloneRecord(REVIEW_SNAPSHOT);
    const attachments = array(record(record(attachmentSwap.claimPacket).claim).attachments);
    [attachments[0], attachments[1]] = [attachments[1], attachments[0]];
    expect(() => parseWorkflowSnapshot(attachmentSwap)).toThrow(/image localRefs/);
  });
});

describe("closed workflow event parser", () => {
  it("accepts content-free canonical events", () => {
    expect(parseWorkflowEventEnvelope(SHOWCASE_EVENTS[0]).event.kind).toBe("plan_step");
    expect(parseWorkflowEventEnvelope(QUOTA_EVENT).event.kind).toBe("operational_failure");
  });

  it("rejects raw details, wrong cursors, bool-as-number, and malformed timestamps", () => {
    const raw = cloneRecord(QUOTA_EVENT);
    record(raw.event).raw = { prompt: "secret" };
    expect(() => parseWorkflowEventEnvelope(raw)).toThrow(/not an allowed field/);

    const cursorMismatch = cloneRecord(QUOTA_EVENT);
    cursorMismatch.cursor = 8;
    expect(() => parseWorkflowEventEnvelope(cursorMismatch)).toThrow(/sourceAuditSequence/);

    const boolCursor = cloneRecord(QUOTA_EVENT);
    boolCursor.cursor = true;
    expect(() => parseWorkflowEventEnvelope(boolCursor)).toThrow(/integer/);

    const badTime = cloneRecord(QUOTA_EVENT);
    badTime.occurredAt = "2026-07-14T24:00:00Z";
    expect(() => parseWorkflowEventEnvelope(badTime)).toThrow(/clock/);
  });

  it("accepts exactly retryable, non-terminal provider failures for one retry", () => {
    for (const category of [
      "timeout",
      "provider_unavailable",
      "invalid_response",
    ]) {
      const retry = cloneRecord(SHOWCASE_EVENTS[2]);
      Object.assign(record(record(retry.event).failure), {
        category,
        retryable: true,
        terminal: false,
      });
      expect(parseWorkflowEventEnvelope(retry).event.kind).toBe("retry");
    }

    for (const failure of [
      { category: "timeout", retryable: false, terminal: true },
      { category: "invalid_response", retryable: false, terminal: false },
      { category: "provider_unavailable", retryable: true, terminal: true },
    ]) {
      const retry = cloneRecord(SHOWCASE_EVENTS[2]);
      Object.assign(record(record(retry.event).failure), failure);
      expect(() => parseWorkflowEventEnvelope(retry)).toThrow(
        /retryable|non-terminal|terminal/,
      );
    }
  });

  it("requires a deterministic or model signal for verification mismatch events", () => {
    const unsupportedMismatch = cloneRecord(SHOWCASE_EVENTS[5]);
    record(unsupportedMismatch.event).status = "mismatch";
    expect(() => parseWorkflowEventEnvelope(unsupportedMismatch)).toThrow(
      /mismatch event requires a mismatch signal/,
    );

    const deterministicMismatch = cloneRecord(unsupportedMismatch);
    record(deterministicMismatch.event).deterministicMatch = false;
    expect(parseWorkflowEventEnvelope(deterministicMismatch).event.kind).toBe(
      "verification",
    );

    const modelMismatch = cloneRecord(unsupportedMismatch);
    record(modelMismatch.event).modelReportedMismatch = true;
    expect(parseWorkflowEventEnvelope(modelMismatch).event.kind).toBe(
      "verification",
    );
  });
});

function cloneRecord(value: unknown): Record<string, unknown> {
  return record(structuredClone(value));
}

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Test fixture is not an object");
  }
  return value as Record<string, unknown>;
}

function array(value: unknown): unknown[] {
  if (!Array.isArray(value)) throw new Error("Test fixture is not an array");
  return value;
}

function setG4Conflict(snapshot: Record<string, unknown>): void {
  const gates = array(record(snapshot.claimPacket).gateDecisions);
  Object.assign(record(gates[4]), {
    deterministicPassed: false,
    modelBlocked: false,
    passed: false,
    reasonCodes: ["G4_CONFLICTING_SOURCES"],
  });
}

function setCompoundG8Failure(
  snapshot: Record<string, unknown>,
  reasonCodes: readonly string[],
): void {
  const packet = record(snapshot.claimPacket);
  applyCompoundVerificationReport(record(packet.verification));
  const packetGate = record(array(packet.gateDecisions)[8]);
  packetGate.modelBlocked = reasonCodes.includes("G8_MODEL_MISMATCH");
  packetGate.reasonCodes = [...reasonCodes];

  const attempts = array(record(snapshot.verificationAttempts).attempts);
  const attempt = record(attempts[0]);
  applyCompoundVerificationReport(record(attempt.report));
  record(record(snapshot.portalSession).fields).attachments = structuredClone(
    array(record(attempt.report).actualAttachmentIds),
  );
  const attemptGate = record(attempt.gateDecision);
  attemptGate.modelBlocked = reasonCodes.includes("G8_MODEL_MISMATCH");
  attemptGate.reasonCodes = [...reasonCodes];
}

function applyCompoundVerificationReport(report: Record<string, unknown>): void {
  report.actualAttachmentCount = 2;
  report.actualAttachmentIds = array(report.expectedAttachmentIds).slice(0, 2);
  report.deterministicMatch = false;
  report.fieldResults = array(report.fieldResults).filter(
    (field) => record(field).field !== "incident_time",
  );
  report.modelReportedMismatch = true;
  report.reviewAllowed = false;
  report.status = "mismatch";
}
