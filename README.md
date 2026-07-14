# ClaimDone

> ClaimDone is a local Build Week sandbox prototype. It does not connect to an insurer and cannot
> submit, approve, price, or adjudicate a real insurance claim.

ClaimDone explores whether a constrained assistant can turn user-provided evidence into a
traceable claim draft, ask only for information that is actually missing, and fill a local sandbox
portal up to a human review boundary. Deterministic gates, explicit provenance, and server-owned
workflow state are authoritative; model output, browser content, and UI state are not.

## Repository status

INT-001 is integrated and locally verified at integration anchor
`ae2763bff760114a82bfb23620bcf4d01723466e`. The evidence is scoped to the deterministic,
no-live-AI walking skeleton: canonical checks passed, both services ran together, and the text and
synthetic-WAV paths each reached the intended review boundary. This is implementation evidence,
not release approval or a product-quality benchmark.

| Area | Status on the INT-001 integration base |
| --- | --- |
| Pinned Node.js, pnpm, Python, and uv setup | **Implemented** |
| Canonical Pydantic contracts, generated JSON Schema, and generated TypeScript types | **Implemented** |
| Case service, SQLite persistence, version checks, and redacted audit primitives | **Implemented and composed in FastAPI** |
| Media validation and deterministic gates G0-G5 | **Implemented, composed, and covered by negative tests** |
| Product shell, disclosure, intake preflight, and local accessibility states | **Implemented and wired to server-owned responses** |
| Local sandbox portal with variants A/B and server-owned transitions | **Implemented; INT-001 verified variant A** |
| EVAL-001 static dataset and validator | **Implemented: exactly 12 synthetic cases, included in canonical Make checks** |
| No-live-AI walking skeleton | **Locally verified in two rounds, before and after reset; each covered text and synthetic-WAV paths** |
| GPT-5.6, transcription, Computer Use, independent G8 verification, and G9/G10 human authority | **Planned for later waves** |
| Deterministic eval runners, reports, and G11 release gate | **Planned; EVAL-001 is a dataset and validator, not a measured product-quality report** |

Implementation order and ownership are tracked in
[CLAIMDONE_IMPLEMENTATION_TASKS.md](CLAIMDONE_IMPLEMENTATION_TASKS.md). The design, gate, and
evaluation intent is recorded in [CLAIMDONE_BUILD_WEEK_PLAN.md](CLAIMDONE_BUILD_WEEK_PLAN.md).

## Problem and scope

Capturing evidence after an incident can be stressful, while forms require precise facts that may
come from different sources. The narrow MVP question is whether ClaimDone can prepare a neutral,
source-linked draft and operate a local demo portal without inventing facts or crossing the human
approval boundary.

The current INT-001 walking skeleton intentionally uses a deterministic mock packet instead of a
live model:

1. disclose that the experience is a sandbox;
2. accept exactly three synthetic JPG/PNG images, either a text statement or a PCM WAV recording,
   all three consents, and one explicit EXIF decision per image;
3. run deterministic intake/privacy checks before exposing input to the mock extraction boundary;
4. validate the mock output through G2-G5 and ask exactly one clarification;
5. apply the answer through a server-owned, version-checked clarification flow;
6. fill local sandbox portal variant A and move that portal to `review`; and
7. stop the backend case at `verifying` with verification still `pending`.

That last boundary is intentional. INT-001 does not claim that G8 verification or human approval
has happened, and it must not move the backend case to its later `review` state. Live AI, portal
variant B automation, independent verification, approval, and receipts remain later work.

Out of scope for the MVP:

- real insurer portals, credentials, policies, or customer data;
- autonomous submission, approval, payment, booking, or contact actions;
- liability, legal, coverage, repair-cost, or damage-value decisions;
- production hosting, multi-tenant operation, and a second claim scenario; and
- fixed-coordinate browser automation presented as adaptive Computer Use.

## Architecture and authority

ClaimDone is a pnpm/uv monorepo with a Next.js web surface and a FastAPI service. Cross-runtime data
structures originate in `contracts/`. The INT-001 composition assigns workflow state, gates,
persistence, and mock orchestration to FastAPI; Next.js owns the product and local sandbox user
interfaces.

See [docs/architecture.md](docs/architecture.md) for diagrams, trust boundaries, and the
implemented-versus-planned component map. Canonical contract documentation is in
[contracts/README.md](contracts/README.md); generated files under `contracts/generated/` must not
be edited by hand.

### Deterministic gates

The runtime implementations for G0-G5 are present in Welle 1. G0 validates count, bytes, format,
input mode, audio duration, and consent. G1 enforces the explicit privacy/EXIF decisions. G2-G5
validate structured output, safety/scope, provenance, and completeness. Every gate decision is
immutable, and a failed deterministic decision stops downstream work.

G6-G8 tool authority, portal-write authorization, and independent rendered-value verification are
planned for Welle 2. G9-G10 human approval and receipt redaction, and the G11 release gate, are also
planned. Their contracts do not make those runtime controls implemented.

The governing rule applies now and later: a model or UI may add a block, but it may never clear,
replace, or weaken a deterministic failure.

## Prerequisites

Use these exact versions:

- Node.js `24.14.0`
- pnpm `11.7.0`
- Python `3.12.13`
- uv `0.8.3` (bootstrapped inside the repository by `make setup`)

The scripts first look on `PATH`, then use the bundled Codex runtime when available. Never commit
machine-specific runtime paths.

## Setup

From the repository root:

```bash
make check-runtime
make setup
```

`make setup` installs the frozen pnpm and uv environments and is designed to be idempotent. It may
need network access when the pinned dependencies are not cached. `.env.example` documents the
loopback origins used by the local integration. No OpenAI key is needed for INT-001; do not
put a real key in a committed file.

At the recorded anchor, the lock resolves `python-multipart==0.0.32` and production
`httpx==0.28.1`. The first successful setup installed one unavailable package after approved
network access; the second setup audited 27 packages and produced no tracked diff.

## Run the local integration

Start both processes from the repository root:

```bash
make dev
```

The canonical local origins are:

- product and sandbox web: <http://127.0.0.1:3000>
- API: <http://127.0.0.1:8000>
- web health: <http://127.0.0.1:3000/health>
- API health: <http://127.0.0.1:8000/health>

The integrated route <http://127.0.0.1:3000/claim/new> returned HTTP `200` in the recorded local
run. To reproduce the user flow, acknowledge the sandbox disclosure, use only staged synthetic
media, submit exactly three images plus one statement mode, answer the single clarification, and
follow the server-provided link to `/sandbox/A/cases/{caseId}`. The recorded evidence used direct
HTTP requests against both running services; it was not a browser-based visual or accessibility
approval.

The frozen integration transport is:

- multipart `POST /api/cases/{caseId}/intake` with a positive `expectedVersion` checked before any
  media/mock work, `images` exactly three times, optional `statementText` XOR one WAV `audio`, three
  consent booleans, and `exifDecisions` exactly three times; and
- `POST /api/cases/{caseId}/clarifications/{clarificationId}/answer` with
  `{ "expectedVersion": <integer>, "answer": <text> }`.

Successful integration responses bind `requestId`, `case`, `draftRevision`, `gateHistory`, `phase`,
`clarification`, and `portal`. After the one answer, the required boundary is backend
`case.state=verifying`, portal `portalState=review`, and `verificationState=pending`. Both the text
and synthetic-WAV walkthroughs reached those three values together; request IDs and the scoped
evidence are recorded in [docs/verification-results.md](docs/verification-results.md).

Stop `make dev` with `Ctrl-C`.

## Test

After `make setup`, run the same targets locally and in CI:

```bash
make lint
make typecheck
make test
```

`make lint` covers shell syntax, ESLint, and Python including `evals/`. `make typecheck` runs strict
TypeScript and mypy checks including `evals/`. `make test` runs Vitest and pytest, and pytest
discovers `services/api/tests`, `scripts/tests`, and `evals/tests`. The EVAL-001 validator rejects a
dataset with anything other than exactly twelve cases. No live model or network service is invoked
by these eval checks.

At the INT-001 anchor, `make lint` and `make typecheck` passed, with mypy checking 64 Python source
files, and `make test` passed with 87 Vitest and 264 pytest tests. The pinned-runtime production
build also passed. It can be reproduced without a machine-specific runtime path with:

```bash
bash -c 'source scripts/runtime.sh; claimdone_resolve_runtime; "$CLAIMDONE_PNPM_BIN" build:web'
```

## Reset

Stop the development processes, then run:

```bash
make reset
```

The reset command removes only generated caches and repository-local runtime state, including the
default `.local/claimdone/` SQLite/media directory. It preserves environment files, installed
dependencies, source files, fixtures, and tool installations, and is safe to run repeatedly.

INT-001 also composes two application-level reset surfaces: backend `POST /api/dev/reset` clears
ClaimDone-owned demo cases/media, while the portal's visible **Reset fixture** action calls the web
`POST /api/dev/reset` route for its own fixture state. `DELETE /api/cases/{caseId}` is the
case-scoped backend cleanup. The integrated walkthrough verified case/media and portal deletion,
then observed both developer resets report zero remaining entries after cleanup. The repository
reset removed 22 generated entries on its first run and zero on its second, while preserving
environment files, dependencies, source files, fixtures, and tool installations; the services and
full walkthrough passed again after restart. Do not assume one service's reset implicitly clears
the other service; use `make reset` with both processes stopped for a clean repository-local
restart.

## Evaluation

`evals/dataset.json` contains exactly twelve static, synthetic EVAL-001 cases across German and
English happy paths, missing fields, uncertainty, safety, and injection. They validate the expected
contract and gate decisions without running the product, a browser, or a model. That is useful
regression input, but it is not an end-to-end score.

Deterministic graders, product-flow runners, model graders, versioned reports, and the G11 release
decision are planned for EVAL-002 and later tasks. Model graders may eventually add failures for
qualitative properties; they will never be allowed to override deterministic graders or gates.

Measured INT-001 commands, runtime versions, commit SHA, and walkthrough observations are recorded
in [docs/verification-results.md](docs/verification-results.md). Unmeasured product-quality,
model-graded, security, human, and release checkpoints remain explicitly `PENDING`; `PENDING` is
never evidence of a pass.

## How Codex is used

Codex is an implementation collaborator. Tasks are assigned to focused `codex/*` branches and Git
worktrees, reviewed against directory ownership, and handed back with commands and commit SHAs.
Repository verification uses the same Make targets locally and in CI.

Codex is not a runtime authority in ClaimDone. Agent-authored code, plans, UI state, browser
content, and model output remain subordinate to deterministic contracts and gates.

## Planned OpenAI use

No live OpenAI request is part of INT-001. Later tasks plan GPT-5.6 through the Responses API for
constrained evidence extraction, a visible bounded plan, local sandbox Computer Use, and a
supplementary verification signal. A separate transcription model is planned for short audio.

Those outputs must use strict structured contracts. Models must not infer identity, policy,
address, registration, VIN, liability, coverage, payment, or cost facts from images. Live model
names, prompts, settings, retry behavior, cost, latency, and measured outcomes will be documented
only after the adapters and corresponding runs exist.

## Limitations

- INT-001 evidence is local and scoped to the deterministic walking skeleton at the recorded
  integration anchor; it is not a release decision.
- The current walking skeleton uses a deterministic mock packet and portal API calls, not live AI
  or adaptive Computer Use.
- Portal `review` plus backend `verifying` and verification `pending` is an intermediate boundary,
  not verified review, approval, submission, or a receipt.
- EVAL-001 validates 12 expected cases structurally; it does not execute them against the product.
- Only staged synthetic fixtures are permitted. Never use real insurance information, genuine
  credentials, or identifying media.
- The user approved the visual direction. V1 is intentionally code-first: tokens, components,
  states, and accessibility behavior live with the frontend, and a Figma artifact is not required.
  A complete accessibility review is still pending.
- Security, accessibility, performance, external product testing, repository licensing, and
  submission authorization remain pending their listed human or later-wave tasks.

## Documentation map

- [Architecture and trust boundaries](docs/architecture.md)
- [INT-001 verification results and remaining checkpoints](docs/verification-results.md)
- [Evaluation dataset](evals/README.md)
- [Canonical contracts](contracts/README.md)
- [Build Week plan](CLAIMDONE_BUILD_WEEK_PLAN.md)
- [Implementation task list](CLAIMDONE_IMPLEMENTATION_TASKS.md)
