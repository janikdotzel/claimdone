# ClaimDone canonical contracts

Contract version: **4.0.0**

The Pydantic models in
`services/api/src/claimdone_api/contracts/` are the only canonical Python
definitions. They use camelCase aliases on the wire, reject unknown fields and
coercion, and are immutable after validation. Frontend, backend, portal,
Computer Use, gates, and eval code must consume these contracts rather than
creating local lookalikes.

## Generated artifacts

- `generated/claimdone.schema.json` is the JSON Schema 2020-12 catalog. Its
  dialect is compatible with OpenAPI 3.1.
- `generated/claimdone.ts` is rendered only from that schema and exposes
  readonly TypeScript types.

Generate or check the artifacts from the repository root:

```bash
PYTHONPATH=services/api/src .venv/bin/python -m claimdone_api.contracts.generate
PYTHONPATH=services/api/src .venv/bin/python -m claimdone_api.contracts.generate --check
```

Do not edit files under `generated/` manually. The drift test compares their
exact bytes and separately proves that the committed TypeScript is reproduced
from the committed JSON Schema.

## State and authority invariants

The text-input path is:

```text
created → disclosed → analyzing → awaiting_clarification → ready_to_fill
        → filling → verifying → review → human_approved → receipt
```

`analyzing` may go directly to `ready_to_fill` when no clarification is
needed. Audio has an additional fail-closed branch before any AI analysis:

```text
created → disclosed → awaiting_transcript_confirmation → analyzing
```

The confirmation view and request bind the human decision to the exact case,
transcript ID, SHA-256, and optimistic version. The existing INT-001
walking-skeleton mock is local and deterministic. For audio it stores the
owned transcript, enters the
confirmation state, and stops before extraction or G2; it is not an OpenAI
consumer and does not implement the future confirmation endpoint. Both
`ModelExtraction` and `ClaimPacket` reject `transcriptConfirmed=false` or
`null`. AI-001 must add the human-confirmation adapter before any provider call
and resume analysis only with the confirmed artifact.

`blocked`, `emergency_stopped`, `abandoned`, and `failed` are explicit stop
paths. `blocked` never transitions to `human_approved`; only `review` may do so.
The generated schema contains the complete transition map.

`ClaimScope.agentCanSubmit` and `ToolPlan.agentCanSubmit` are the literal value
`false` in Python, JSON Schema, and TypeScript. A gate passes exactly when its
deterministic result passes and no permitted model signal adds a block. A
release passes only when deterministic checks, model-quality thresholds, and
human checkpoints all pass. Model output can therefore add a failure but
cannot override a deterministic one.

`ToolInvocation` contains a trusted invocation ID, a closed tool enum, and an
exactly empty argument object. Case IDs, URLs, and field values are resolved
from trusted server state and cannot appear in a model plan or invocation
arguments. G0-G10 are registered in one immutable runtime registry; G11 remains
the separate release gate.

`WorkflowCaseView`, `ClarificationView`, `ClarificationAnswerRequest`, and
`WorkflowSnapshot` are the closed HTTP workflow roots. The snapshot contains
no raw intake summary or arbitrary metadata map. Active transcript and
clarification payloads are state-exclusive and optimistic-version-bound.
The state matrix requires a `ClaimPacket` from `awaiting_clarification`
through `human_approved`, except that `analyzing` may contain either no packet
or its same-state packet. Pre-extraction and receipt states cannot expose one.
Portal sessions exist only in ready/filling, verifying/review, or terminal
stop snapshots; their draft/review state is fixed by the case state.
Verification series exist only in verifying/review or a terminal stop and can
never be orphaned from a packet. `verifying` intentionally allows
`verificationAttempts: null` until a series has completed.

Review requires a case-bound review packet, review portal session, exact
portal values byte-for-byte equal to the packet's canonical JSON claim fields,
and a complete final successful G8 attempt series. Receipt state exposes only
the redacted `SandboxReceipt`; it cannot expose packet, portal, verification,
transcript, or clarification data. Clarification answers are kept byte-for-byte
(including surrounding whitespace) only in the closed action DTO and never in
workflow events; deterministic normalization belongs in the service layer.

These state/nullability rules and the tool-call status/duration variants are
emitted as JSON Schema `oneOf` branches and therefore as TypeScript unions.
Relational equality of nested case IDs, optimistic/portal versions, packet
state, and final G8 remains server/runtime validated in addition to the
cross-runtime shape constraints.

`WorkflowEventEnvelope` is a redacted read projection bound to the canonical
audit event ID, type, and sequence. Every workflow kind has exactly one matching
audit type; downstream OBS must write the audit truth and projection atomically.
Its closed event union has no prompt, response,
transcript, image, tool argument, remote error text, or arbitrary details map.
Operational provider failures are not gates. Quota, billing, rate, auth,
permission, invalid-request, model-not-found, content-filter, and cancellation
failures are terminal and non-retryable; only the explicitly bounded
extraction retry may use a retry event. Successful calls, retries, and terminal
operational failures share the same closed operation/model/provider-mode/retry
binding, call sequence, and duration for the same attempted call. Tool
durations are absent while a tool is started and required only on terminal
succeeded or blocked events. Live transcription is bound to
`gpt-4o-transcribe`; every live non-transcription workflow operation is bound
exactly to `gpt-5.6-sol`. Terra and Luna remain reserved enum identities for
future independent evaluation surfaces, not workflow telemetry.

Portal drafts preserve bounded raw controls, while review values and rendered
verification snapshots remain separate contracts. Receipt projection is only
the closed, redacted `SandboxReceipt`; a portal session cannot expose claim
fields in receipt state. Verification compares the exact ordered attachment
IDs as well as redundant attachment counts. The expected IDs remain bound to
`ClaimData.attachments`; actual IDs remain bound to the freshly rendered portal
values. A same-count wrong or reordered list therefore fails G8. Verification
permits at most one evidence-linked scalar repair, attachment identity cannot
change during that repair, and only a final attempt may emit G8. Attachment IDs
are exact raw wire values and unique within every attachment list, so whitespace
normalization or repeating one physical reference cannot satisfy multiple slots.

Eval result roots bind runs to a dataset SHA-256 and Git commit, require every
closed metric aggregate, derive those aggregates from case checks, and require
zero provider calls in deterministic mode.

Equal JSON Schema `minItems`/`maxItems` constraints are emitted as readonly
TypeScript tuples (for example the three claim attachments). Unequal minimum or
maximum array bounds remain runtime JSON Schema/Pydantic constraints because
TypeScript cannot express them without impractical recursive tuple types.

## Compatibility policy

The contract follows conservative semantic versioning:

- **Major:** any change that alters whether an existing JSON instance is valid,
  including field names or requiredness, enum values, validation invariants,
  transition rules, provenance rules, or authority boundaries.
- **Minor:** a new independent root model or non-validating schema metadata that
  leaves every existing root's accepted instance set unchanged.
- **Patch:** documentation, generator formatting, or implementation fixes that
  produce no wire-schema change.

Every schema change must update `CONTRACT_VERSION`, regenerate both artifacts,
and run the complete contract test suite. Consumers must update from the
generated schema and TypeScript artifact in the same integration commit.

Version 2.0.0 was the major boundary that added case states and enum values,
tightened gate authority, closed persisted details/tool arguments, and added
new validation invariants. Version 3.0.0 added the canonical HTTP workflow
roots and intentionally invalidated older transcript and telemetry roots
through required case identity, state-matrix, provider-binding, and duration
rules.

Version 4.0.0 is the attachment-identity authority boundary. Verification
reports now require ordered `expectedAttachmentIds` and nullable ordered
`actualAttachmentIds`; their redundant counts must be jointly present and
equal the corresponding list lengths. Exact ID equality controls deterministic
verification, G8, and repair eligibility. Every root carries the exact `4.0.0`
literal. Persisted 1.x, 2.x, or 3.x payloads are neither accepted nor relabelled
as v4. This contract wave does not add a persistence migration: integration
must explicitly choose a safe local reset or a real validated migration before
older payloads are read.

## Examples

`examples/happy_path.json`, `examples/block.json`, and
`examples/mismatch.json` are synthetic, non-sensitive `ClaimPacket` fixtures.
They are validated as part of the API tests and are safe for deterministic
development and demos.
