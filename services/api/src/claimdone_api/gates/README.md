# ClaimDone G0-G10 gate boundary

`G0_TO_G10_REGISTRY` is the canonical ordering and reason-priority authority
for every runtime gate. `G0_TO_G5_REGISTRY` is a derived compatibility prefix
for the implemented intake pipeline. Callers append the final decision for
each gate to a fresh history and must stop whenever `decision.passed` is false.
There is no API that accepts a UI- or model-provided pass value. G11 remains the
separate release gate and is intentionally absent from this runtime registry.

- G0/G1 are executed by `claimdone_api.media`; they use the same registry
  decision constructor and expose model-ready paths only after G1 passes.
- G2 accepts a provider-normalized extraction DTO and an independently built
  tuple of approved `EvidenceItem` values. Authority fields such as case/state,
  gates, plan and verification are not part of the model schema. It rejects
  unknown/duplicate fields, invented references, refusals and truncation.
  `OutputContractRun` preserves the initial attempt and at most one retry; only
  its final result belongs in the authoritative gate history. A retry is valid
  only when evaluation receives the run containing its retryable failed first
  attempt.
- G3 receives explicit safety/scope facts. Its optional model signal can add
  `G3_MODEL_UNCERTAIN`, while `safe` cannot remove any deterministic reason.
- G4 derives its entire inventory from the authoritative `ClaimPacket`; callers
  cannot select a fact subset. Every packet fact is audited and every populated
  non-attachment claim field requires supported facts whose value exactly
  equals the canonical field and whose source union exactly equals that field's
  provenance. Narrative requires an exact `narrative` fact; wrong-field facts
  never authorize its text. Attachments are bound to the exact three approved
  image references. Any G4 failure is a transaction-wide write barrier.
- G5 receives the immutable G4 result instead of caller-provided conflict
  fields, recomputes missing fields from its bound claim snapshot, and accepts
  zero or one structured question for the first real blocker. Rounds 0, 1 and 2
  may ask; after three completed rounds it requires manual handoff. A failed G4
  conflict and its one bound G5 question live in `ClarificationSubflow`, outside
  authoritative gate history; a new packet must rerun the gates.
- G6 accepts the raw closed `ToolInvocation` payload plus trusted runtime state.
  It authorizes only the packet's exact `fill_until_review` plan step, the exact
  canonical loopback case/variant URL, a closed browser-action kind, state
  `filling`, a reserved one-based proposed-action number from 1 through 40,
  and an inclusive 90-second limit. Approval,
  submission, receipt, reset, delete, unknown tools, arguments, or paths always
  add a deterministic block.
- G7 evaluates the complete portal field payload atomically. The closed wire
  field set, strict scalar types and values, all canonical provenance, state
  `filling`/`draft`, and the exact ordered three attachment IDs must match the
  packet. The complete packet is round-trip revalidated before any expected
  value is derived; invalid trusted input raises one content-free error.
  Rejected values are never retained in the result.
- G8 revalidates the packet and fresh rendered snapshot, binds case, variant and
  portal version, and requires the rendered timestamp to fall within its
  trusted request/receive interval of at most five seconds and before the
  verification timestamp. It then compares every scalar plus raw ordered attachment
  identity. Scalar normalization is limited to line-ending normalization,
  outer whitespace and conservative ISO date/time parsing. The model mismatch
  boolean can add `G8_MODEL_MISMATCH`; it cannot remove deterministic reasons.
  Forged, stale or foreign identity inputs raise one content-free input error
  and never produce a verification report.

Integration must preserve the portal session's G7-authorized fields in their
raw exact form. G8's normalized comparison values and report are independent
derived evidence and must never replace or rewrite that `PortalSession` state.

Every result object may contain diagnostic values for display, but downstream
work is authorized only by its immutable `GateDecision.passed` value.
