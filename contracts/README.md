# ClaimDone canonical contracts

Contract version: **1.0.0**

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

The normal path is:

```text
created → disclosed → analyzing → awaiting_clarification → ready_to_fill
        → filling → verifying → review → human_approved → receipt
```

`blocked`, `emergency_stopped`, `abandoned`, and `failed` are explicit stop
paths. `blocked` never transitions to `human_approved`; only `review` may do so.
The generated schema contains the complete transition map.

`ClaimScope.agentCanSubmit` and `ToolPlan.agentCanSubmit` are the literal value
`false` in Python, JSON Schema, and TypeScript. A gate passes exactly when its
deterministic result passes and no permitted model signal adds a block. A
release passes only when deterministic checks, model-quality thresholds, and
human checkpoints all pass. Model output can therefore add a failure but
cannot override a deterministic one.

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

## Examples

`examples/happy_path.json`, `examples/block.json`, and
`examples/mismatch.json` are synthetic, non-sensitive `ClaimPacket` fixtures.
They are validated as part of the API tests and are safe for deterministic
development and demos.
