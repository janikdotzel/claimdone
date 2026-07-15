# ClaimDone

> ClaimDone is a local Build Week sandbox prototype. It does not connect to an insurer and cannot
> submit, approve, price, or adjudicate a real insurance claim.

ClaimDone prepares a traceable draft from staged evidence, asks only for a fact that is actually
missing, fills a local sandbox portal, and independently verifies the rendered values before
stopping at a human review boundary. Deterministic gates, provenance, and server-owned workflow
state are authoritative; model output, browser content, and UI flags are not.

## V1 status

The deterministic V1 is scoped through INT-002 and currently remains an acceptance candidate. Its
eventual acceptance is bound to the unchanged commit containing
[the V1 test handoff](docs/v1-test-handoff.md) and the versioned
[normalized evidence](docs/evidence/int002-v1-acceptance.json), after the two counted browser runs
and all final Make gates have passed.

| Area | Deterministic V1 status |
| --- | --- |
| Pinned pnpm/uv monorepo and canonical Make targets | Implemented |
| Canonical contracts and server-owned state/version authority | Implemented |
| Synthetic intake, local media checks, consent, and EXIF decisions | Implemented |
| Deterministic analysis and exactly one `incident_time` clarification | Implemented |
| G0-G8, portal-run authority, field-write authority, and verification | Implemented |
| Intended mismatch, one narrow repair, second successful verification | Implemented |
| Product UI and read-only Portal A review | Implemented |
| Agent approval, submission, and receipt | Deliberately unavailable in the V1 flow |
| External OpenAI calls, audio, free-form fixtures, and Portal B acceptance | Out of scope |
| W3, release, submission, hosting, and production | Not started |

The candidate path is fixture-only. It is a local deterministic test MVP, not evidence of
live-model quality, statistical reliability, release readiness, or production safety.

## V1 flow

1. The user acknowledges the sandbox and human-approval boundary.
2. The user selects the three generated fixture PNGs in manifest order, retains metadata for each,
   pastes the exact synthetic statement, and confirms all three consents.
3. The backend creates case v1, commits intake and analysis, then returns exactly one clarification
   at `awaiting_clarification` v4.
4. The answer `14:30:00` produces `ready_to_fill` v5.
5. The protected Portal A run applies G6 and G7, introduces the rehearsed `incident_time` mismatch,
   blocks review, performs one authorized repair, and verifies again through G8.
6. The backend stops at `review` v9; Portal A is at `review` v4. No agent approval or receipt exists.

Only `claimdone-int002-main-v1` is eligible for this acceptance. The server rejects different image
bytes, a modified statement, audio, a different EXIF decision, or a different clarification answer.

## Authority boundaries

- G0 validates exact media count, byte signatures, statement mode, size, and consent.
- G1 requires an explicit EXIF decision for each image.
- G2-G5 enforce strict output, safety/scope, provenance, and completeness.
- G6 authorizes only the bounded local tool sequence and loopback Portal A.
- G7 binds every portal write and all three attachments to approved claim evidence.
- G8 performs a fresh rendered-value comparison. A deterministic mismatch cannot be overridden by
  a model, browser, UI, or human assertion.
- The V1 flow stops before G9/G10 execution. Agent capabilities cannot approve or obtain a receipt.
- G11 release readiness is outside this scope and is not represented as passed.

See [the architecture](docs/architecture.md) and
[computer-use security boundary](docs/computer-use-security.md) for details.

## Prerequisites

Use these exact versions:

- Node.js `24.14.0`
- pnpm `11.7.0`
- Python `3.12.13`
- uv `0.8.3`, bootstrapped repo-locally by `make setup`

The scripts first resolve exact versions on `PATH`, then the bundled Codex runtime when available.
Never commit machine-specific runtime paths.

## Setup

From the repository root:

```bash
make check-runtime
make setup
```

`make setup` installs the frozen pnpm and uv environments and is idempotent. Network access is only
needed if dependencies are not cached. No OpenAI key is required for the deterministic V1.

## Generate the fixture

The three PNG files are generated into the ignored `.local/int002-fixtures/` directory:

```bash
./.venv/bin/python scripts/generate_int002_fixtures.py
./.venv/bin/python scripts/generate_int002_fixtures.py --check
```

The canonical hashes and the one clarification answer are in
[`fixtures/int002/manifest.json`](fixtures/int002/manifest.json). All fixture data is synthetic and
non-identifying.

## Run

```bash
make dev
```

- Product and portal: <http://127.0.0.1:3000>
- API: <http://127.0.0.1:8000>
- Product flow: <http://127.0.0.1:3000/claim/new>
- Web health: <http://127.0.0.1:3000/health>
- API health: <http://127.0.0.1:8000/health>

Stop both services with `Ctrl-C`. Follow the exact user flow and expected results in
[docs/v1-test-handoff.md](docs/v1-test-handoff.md).

## Verify

```bash
make check-runtime
make lint
make typecheck
make test
make eval-deterministic
```

The deterministic eval runner executes without a live model or paid request. Production gate logic
remains outside `evals/`; the eval runner can report a failure but cannot mutate runtime authority.

## Reset

Stop `make dev`, then run:

```bash
make reset
```

Reset removes only generated caches and `.local` runtime state. It preserves environment files,
dependencies, source, fixtures, and tool installations. Run it before each formal INT-002 attempt.

## Security and privacy

- Use only the included staged fixture. Never use real insurance information, credentials, or
  identifying media.
- Raw image bytes and full claim values are not written to workflow events.
- The browser policy accepts only the configured IPv4 loopback portal origin and closed tool set.
- The local browser boundary is not an independent OS/container egress sandbox.
- The agent cannot approve, submit, pay, contact a third party, or obtain a receipt in V1.

## Limitations

- The candidate path is fixed to one exact statement, three exact PNGs, and Portal A.
- External providers and transcription are disabled; the persisted mock provider event is not a
  live request.
- The application uses a semantic local Playwright adapter, not an accepted OpenAI Computer Use
  Responses loop.
- Two identical counted runs will prove reproducibility only for the named fixture.
- Human accessibility review, Portal B reliability, broader security evaluation, performance,
  licensing, video, submission, release, and production remain future decisions.
- Figma is intentionally not a V1 dependency; the approved design direction is code-first.

## Documentation

- [V1 test handoff](docs/v1-test-handoff.md)
- [Versioned INT-002 evidence](docs/evidence/int002-v1-acceptance.json)
- [Architecture and trust boundaries](docs/architecture.md)
- [Verification history](docs/verification-results.md)
- [Computer-use security boundary](docs/computer-use-security.md)
- [Deterministic evaluations](evals/README.md)
- [Canonical contracts](contracts/README.md)
- [Build Week plan](CLAIMDONE_BUILD_WEEK_PLAN.md)
- [Implementation task list](CLAIMDONE_IMPLEMENTATION_TASKS.md)
