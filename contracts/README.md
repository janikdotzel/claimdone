# ClaimDone canonical contracts

Contract version: **2.0.0**

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

The confirmation request binds the human decision to the exact transcript ID,
SHA-256, and optimistic version. The existing INT-001 walking-skeleton mock is
local and deterministic; it remains a temporary v2 migration bridge and is not
an OpenAI consumer. AI-001 must enforce `transcriptConfirmed=true` before any
provider call and route audio through the confirmation state. This deferred
consumer migration must not be interpreted as provider authority to analyze an
unconfirmed transcript.

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

`WorkflowEventEnvelope` is a redacted read projection bound to the canonical
audit event ID, type, and sequence. Every workflow kind has exactly one matching
audit type; downstream OBS must write the audit truth and projection atomically.
Its closed event union has no prompt, response,
transcript, image, tool argument, remote error text, or arbitrary details map.
Operational provider failures are not gates. Quota, billing, rate, auth,
permission, invalid-request, model-not-found, and cancellation failures are
terminal and non-retryable; only the explicitly bounded extraction retry may
use a retry event.

Portal drafts preserve bounded raw controls, while review values and rendered
verification snapshots remain separate contracts. Receipt projection is only
the closed, redacted `SandboxReceipt`; a portal session cannot expose claim
fields in receipt state. Verification permits at most one evidence-linked
scalar repair and only a final attempt may emit G8.

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

Version 2.0.0 is a required major update because it adds case states and enum
values, tightens gate authority, closes persisted details/tool arguments, and
adds new validation invariants. Version 1.x instances are not accepted as v2.

## Examples

`examples/happy_path.json`, `examples/block.json`, and
`examples/mismatch.json` are synthetic, non-sensitive `ClaimPacket` fixtures.
They are validated as part of the API tests and are safe for deterministic
development and demos.
