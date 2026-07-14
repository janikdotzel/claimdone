# ClaimDone G0-G5 gate boundary

`G0_TO_G5_REGISTRY` is the only ordering and reason-priority authority for the
first six gates. Callers append the final decision for each gate to a fresh
history and must stop whenever `decision.passed` is false. There is no API that
accepts a UI- or model-provided pass value.

- G0/G1 are executed by `claimdone_api.media`; they use the same registry
  decision constructor and expose model-ready paths only after G1 passes.
- G2 accepts provider-normalized raw JSON and an independently built tuple of
  approved `EvidenceItem` values. It rejects unknown/duplicate fields, invented
  references, refusals and truncation. `attempt=0` may request one retry;
  `attempt=1` is the final permitted attempt.
- G3 receives explicit safety/scope facts. Its optional model signal can add
  `G3_MODEL_UNCERTAIN`, while `safe` cannot remove any deterministic reason.
- G4 receives field assertions bound either to an exact canonical
  `ClaimPacket.fact` or directly to user-statement/clarification evidence.
  Attachments are the only direct image-backed field. Callers must not construct
  assertions from UI flags; every `factId`, value, status, source and confidence
  is checked again by the gate.
- G5 recomputes missing fields, adds ordered conflicts, and accepts zero or one
  structured question. With blockers it targets the first canonical field and
  permits rounds 0, 1 and 2; after three completed rounds it requires manual
  handoff.

Every result object may contain diagnostic values for display, but downstream
work is authorized only by its immutable `GateDecision.passed` value.
