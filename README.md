# ClaimDone

> Build Week work in progress. ClaimDone is a local sandbox prototype; it does
> not connect to an insurer and cannot submit, approve, price, or adjudicate a
> real insurance claim.

ClaimDone explores whether a constrained assistant can turn user-provided
evidence into a traceable claim draft, ask only for information that is
actually missing, and fill a local sandbox portal up to a human review
boundary. The project is designed around explicit provenance, deterministic
gates, independent verification, and a final approval action that is outside
the agent's authority.

## Repository status

This README describes both the current repository baseline and the planned
Build Week MVP. Unless a capability is marked **implemented**, treat it as
**planned**, not as a working product claim.

| Area | Status at the DOC-001 baseline |
| --- | --- |
| Pinned Node.js, pnpm, Python, and uv setup | **Implemented** |
| Next.js and FastAPI service shells with `/health` routes | **Implemented** |
| Canonical immutable Pydantic contracts, generated JSON Schema, and generated TypeScript types | **Implemented** |
| Contract tests for state, gate, eval, and release invariants | **Implemented** |
| Case persistence, media intake, runtime gates, and sandbox portal | **Planned; work is not integrated on this baseline** |
| GPT-5.6, transcription, and Computer Use adapters | **Planned; no live model flow is implemented on this baseline** |
| Eval datasets, graders, end-to-end tests, and release gate | **Planned; contract shapes exist, runners and results do not** |
| Human approval and receipt flow | **Planned; no approval endpoint exists on this baseline** |

Implementation order and ownership are tracked in
[CLAIMDONE_IMPLEMENTATION_TASKS.md](CLAIMDONE_IMPLEMENTATION_TASKS.md). The
design, gate, and evaluation intent is recorded in
[CLAIMDONE_BUILD_WEEK_PLAN.md](CLAIMDONE_BUILD_WEEK_PLAN.md).

## Problem

Capturing evidence after an incident can be stressful, while insurance forms
require precise facts that may come from different sources. The ClaimDone MVP
is intended to test a narrow question: can an assistant prepare a neutral,
source-linked draft and operate a local demo portal without inventing facts or
crossing the human approval boundary?

This prototype is not insurance, legal, liability, coverage, payment, or
emergency advice. Safety and scope conditions are intended to stop the demo
rather than produce guidance.

## MVP scope

The planned local demo flow is:

1. disclose that the experience is a sandbox;
2. accept exactly three synthetic JPG/PNG images plus either text or a short
   audio statement and the required consents;
3. create an evidence-linked `ClaimPacket` without inferring protected or
   unsupported facts from images;
4. ask one deterministic clarification at a time when required data is
   missing or contradictory;
5. fill one of two structurally different local sandbox portal layouts;
6. re-read and compare every rendered value against the approved packet; and
7. stop at `review`, where a separate human authority may approve the sandbox
   record.

Only the contracts for this flow are implemented at the DOC-001 baseline. The
end-to-end flow itself is planned.

Out of scope for the MVP:

- real insurer portals, credentials, policies, or customer data;
- autonomous submission, approval, payment, booking, or contact actions;
- liability, legal, coverage, repair-cost, or damage-value decisions;
- production hosting, multi-tenant operation, and a second claim scenario;
- fixed-coordinate browser automation presented as adaptive Computer Use.

## Architecture

ClaimDone is a pnpm/uv monorepo with a Next.js web surface and a FastAPI
service. Cross-runtime data structures originate in one canonical contract
area. In the target architecture, FastAPI owns workflow state, gates, audit
events, model adapters, and browser-runner orchestration; Next.js owns the
product and sandbox user interfaces.

See [docs/architecture.md](docs/architecture.md) for diagrams, trust
boundaries, ownership, and an explicit implemented-versus-planned map.

The canonical contract documentation is in
[contracts/README.md](contracts/README.md). Generated files under
`contracts/generated/` must never be edited by hand.

## Deterministic gates

The contracts define gate IDs `G0` through `G11`, structured reason codes,
immutable `GateDecision` values, workflow transitions, eval expectations, and
release decisions. Runtime execution of the full gate pipeline is still
planned.

The governing rule is already encoded in the contracts: deterministic
failures take precedence over model output, model graders, browser content,
and UI flags. A model may add a block, but it may not clear or weaken a
deterministic failure. The planned pipeline covers:

- intake and privacy (`G0`-`G1`);
- output contract, safety, provenance, and completeness (`G2`-`G5`);
- tool authority, portal writes, and verification (`G6`-`G8`);
- human approval and receipt redaction (`G9`-`G10`); and
- release readiness (`G11`).

## Prerequisites

Use these exact runtime versions:

- Node.js `24.14.0`
- pnpm `11.7.0`
- Python `3.12.13`
- uv `0.8.3` (bootstrapped inside the repository by `make setup`)

The scripts first look on `PATH`, then use the bundled Codex runtime when it is
available. Do not commit machine-specific runtime paths.

## Setup

From the repository root:

```bash
make check-runtime
make setup
```

`make setup` installs the frozen pnpm and uv environments and is designed to
be idempotent. It requires network access when the pinned dependencies are not
already cached.

No OpenAI key is needed for the implemented health shell or deterministic
contract tests. Live model work is planned for a later integration wave. If a
future live workflow is enabled, provide credentials only through the local
environment; `.env.example` contains a placeholder and must never contain a
real secret.

## Run

Start both implemented service shells:

```bash
make dev
```

This starts:

- web: <http://127.0.0.1:3000>
- API: <http://127.0.0.1:8000>
- web health: <http://127.0.0.1:3000/health>
- API health: <http://127.0.0.1:8000/health>

On the DOC-001 baseline, the home page and health responses prove only that
the service shells are running. They do not provide the planned claim flow.
Stop both processes with `Ctrl-C`.

Run the canonical checks with:

```bash
make lint
make typecheck
make test
```

## Reset

```bash
make reset
```

The implemented reset removes generated caches and repository-local runtime
state. It preserves environment files, installed dependencies, source files,
fixtures, and tool installations. A case-level demo reset that also clears
temporary media and application records is planned and must be verified before
the full sample flow is documented as runnable.

## Sample flow

### Available now: deterministic contract examples

The synthetic files in `contracts/examples/` exercise a happy packet, a
blocked packet, and a mismatch packet. `make test` validates them through the
canonical Pydantic models and checks generated-contract drift. They contain no
real people, claims, policies, or identifying media.

### Planned: end-to-end demo

After the walking-skeleton and later integration milestones are complete, this
section will contain a clean-checkout walkthrough from disclosure and intake
to verified portal review, followed by a separate human approval demonstration.
It will include fixture IDs, exact commands, expected states, and reset steps.
Until those steps have been run from the documented commit, this is a plan,
not a verified workflow.

## Evaluation goals

The repository already defines canonical `EvalCase`, expectation, and
release-decision structures. The datasets, result records, and runners are
planned. Target
properties include complete schema validity and provenance for written fields,
zero forbidden facts or tool calls in curated cases, deterministic detection
of portal mismatches, and zero successful agent approvals. These are targets,
not current measurements.

Model graders are planned only for qualitative properties such as neutrality,
question quality, plan clarity, and uncertainty presentation. They may report
additional failures but may not override deterministic graders or gates.

Measured commands, environments, commit SHAs, and artifacts will be recorded
in [docs/verification-results.md](docs/verification-results.md). Empty or
`PENDING` cells in that document are not evidence of a pass.

## How Codex is used

Codex is used as an implementation collaborator: tasks are decomposed in the
implementation plan, assigned to focused `codex/*` branches and Git worktrees,
checked against directory ownership, and handed back with the commands and
commit SHA used for verification. Repository-level checks use the same
`make` targets locally and in CI.

Codex is not a runtime authority in the ClaimDone product. Agent-authored code,
plans, UI state, and model output remain subordinate to the deterministic
contracts and gates. The final project documentation will list the concrete
tasks and artifacts produced with Codex after those changes have been reviewed
and integrated.

## Planned GPT-5.6 use

**Not implemented on the DOC-001 baseline.** The target design uses GPT-5.6
through the Responses API for constrained evidence extraction, a visible
bounded plan, local sandbox Computer Use, and an independent verification
signal. A separate transcription model is planned for short audio.

GPT-5.6 outputs must use strict structured contracts. The model must not infer
identity, policy, address, registration, VIN, liability, coverage, payment, or
cost facts from images. Its planner may select only registered tools, and its
browser runner must stop at `review`. Any model-based verifier or grader may
add a failure, never remove a deterministic one.

Live model names, prompts, request settings, retry behavior, costs, latency,
and measured eval outcomes will be documented only after the corresponding
adapters and runs exist.

## Limitations

- The current baseline is a service and contract foundation, not a complete
  claim-preparation application.
- No live OpenAI request, media pipeline, browser runner, sandbox approval,
  receipt, or end-to-end workflow is available on this baseline.
- Contract definitions for gates and release decisions do not by themselves
  prove that the runtime gate pipeline or release process is implemented.
- Eval thresholds in the plan are goals; no dataset or regression report is
  integrated yet.
- Local synthetic fixtures are the only permitted demo inputs. Never use real
  insurance information, genuine credentials, or identifying media.
- Security, privacy, accessibility, performance, and external product testing
  remain pending until their listed tasks are implemented and reviewed.
- A repository license and public/private sharing decision are pending human
  approval; absence of a license is a release blocker, not permission to reuse
  the code.

## Documentation map

- [Architecture and trust boundaries](docs/architecture.md)
- [Verification-results template](docs/verification-results.md)
- [Canonical contracts](contracts/README.md)
- [Build Week plan](CLAIMDONE_BUILD_WEEK_PLAN.md)
- [Implementation task list](CLAIMDONE_IMPLEMENTATION_TASKS.md)
