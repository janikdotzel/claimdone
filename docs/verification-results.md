# ClaimDone verification results

> Status: **INT-001 WORKING RECORD — NOT RELEASE EVIDENCE**

The Welle-1 source branches are present on the INT-001 base, but the no-live-AI walking skeleton is
still being integrated. A blank, `PENDING`, `NOT RUN`, planned row, or expectation is not a pass. Do
not replace a missing measurement with an estimate, model judgment, screenshot impression, or a
result from a different commit.

## How to record a run

For every checkpoint, record:

- the exact Git commit SHA and whether the worktree was clean before and after the run;
- date, timezone, operating system, and the four required runtime versions;
- fixture or dataset version and whether the run was deterministic or live;
- the exact command, exit code, duration, and output-artifact path;
- skipped checks and why they were skipped;
- failures, retries, redactions, and known limitations; and
- the human reviewer for checkpoints that cannot be automated.

Do not paste secrets, raw media, full names, policy identifiers, vehicle identifiers, private media
storage names, access tokens, or complete prompts containing input data.

## Current evidence boundary

The current repository can make only these scoped implementation statements:

- Welle-1 contains focused case/persistence, media, G0-G5, frontend intake, portal, and contract
  implementations.
- EVAL-001 contains exactly twelve static synthetic cases and a validator. Canonical lint,
  typecheck, and test configuration includes `evals/`.
- INT-001 is specified as a deterministic no-live-AI mock flow with exactly one clarification.
- The required INT-001 end boundary is backend `verifying`, portal `review`, and verification
  `pending`.

None of those statements is a recorded successful integrated walkthrough. The backend and web
composition commits, lockfile update, and one-commit verification must happen before any INT-001
row below becomes `PASS`.

## INT-001 integration checklist

Use one final integration commit for this table. Record commands and state observations verbatim;
do not pre-fill results from a component branch.

| Commit | Check | Expected | Result | Evidence / notes |
| --- | --- | --- | --- | --- |
| `PENDING` | Worktree before setup | Clean, focused INT-001 branch | `PENDING` | `PENDING` |
| `PENDING` | `make check-runtime` | Node 24.14.0, pnpm 11.7.0, Python 3.12.13 | `PENDING` | Include uv check from setup |
| `PENDING` | `make setup` (first run) | Frozen environments install | `PENDING` | Record network/cache use |
| `PENDING` | `make setup` (second run) | Idempotent and no tracked diff | `PENDING` | `PENDING` |
| `PENDING` | `make lint` | Shell, ESLint, ruff, including `evals/` | `PENDING` | `PENDING` |
| `PENDING` | `make typecheck` | Strict TypeScript and mypy, including `evals/` | `PENDING` | `PENDING` |
| `PENDING` | `make test` | Vitest and all configured pytest paths pass | `PENDING` | Must include `evals/tests` |
| `PENDING` | `pnpm build:web` or canonical equivalent | Production Next.js build passes | `PENDING` | Record exact command |
| `PENDING` | `make dev` and both `/health` routes | Both services return expected health response | `PENDING` | Record status codes |
| `PENDING` | `make reset` with services stopped | `.local/claimdone/` and generated caches removed; sources, fixtures, env, deps preserved | `PENDING` | Repeat once to prove idempotence |
| `PENDING` | Restart after reset | Fresh local state without manual DB edits | `PENDING` | `PENDING` |

## INT-001 no-live-AI walkthrough

Use staged synthetic media only. The flow must not require `OPENAI_API_KEY`, an external network
service, a transcription API, or a browser runner.

| Step | Expected deterministic observation | Result | Evidence / notes |
| --- | --- | --- | --- |
| Open `/claim/new` | Disclosure visible; no case can advance on UI assertion alone | `PENDING` | `PENDING` |
| Submit invalid intake | Server G0 or G1 failure blocks mock extraction and portal calls | `PENDING` | Include one negative path |
| Submit valid multipart intake | Positive expected version, exactly 3 JPG/PNG images, text XOR PCM WAV, 3 consents, 3 EXIF decisions accepted | `PENDING` | Version checked before media/mock work; record request ID, never raw values |
| Receive clarification | Exactly one structured, version-bound question | `PENDING` | `PENDING` |
| Submit stale or wrong clarification | HTTP conflict/not-found boundary; no portal mutation | `PENDING` | Record status/code |
| Submit valid clarification once | Packet rebuilt and authoritative G2-G5 path reruns | `PENDING` | `PENDING` |
| Portal fill | Server-provided loopback link opens `/sandbox/A/cases/{caseId}` | `PENDING` | `PENDING` |
| Final INT-001 boundary | Backend `verifying`; portal `review`; verification `pending` | `PENDING` | Must observe all three together |
| Duplicate clarification | Rejected; no second clarification round or duplicate portal mutation | `PENDING` | `PENDING` |
| Case delete | Associated media removed, then case DB rows cascade | `PENDING` | No private storage name in API/audit |
| Backend developer reset | ClaimDone-owned cases, mapped media, and recognized orphans removed | `PENDING` | `PENDING` |
| Portal fixture reset | Portal state returns to its selected fixture | `PENDING` | Separate from backend reset |

## EVAL-001 structural dataset

EVAL-001 is a static expected-case dataset, not an executed product benchmark. Its validator
requires exact contract validity, unique IDs, exactly twelve cases, required category coverage,
synthetic fixture IDs, explicit safety reason codes, and no expected tool execution after a
deterministic pre-tool G3 block.

| Commit | Command | Expected | Result | Evidence / notes |
| --- | --- | --- | --- | --- |
| `PENDING` | `make lint` | Validator and eval tests pass ruff | `PENDING` | Integration commit only |
| `PENDING` | `make typecheck` | Validator and eval tests pass strict mypy | `PENDING` | Integration commit only |
| `PENDING` | `make test` | Dataset tests discover exactly 12 cases without live services | `PENDING` | Integration commit only |

The following are target product metrics from the Build Week plan. They are not measured by loading
the EVAL-001 JSON file.

| Metric | Target | Measured | Report artifact |
| --- | ---: | ---: | --- |
| Strict schema validity | 100% | `PENDING` | EVAL-002 or later |
| Provenance coverage for written fields | 100% | `PENDING` | EVAL-002 or later |
| Forbidden or invented claim facts | 0 cases | `PENDING` | EVAL-002 or later |
| Required-field recall | 100% | `PENDING` | EVAL-002 or later |
| Safety-block recall in the curated set | 100% | `PENDING` | EVAL-002 or later |
| Forbidden tool calls | 0 cases | `PENDING` | EVAL-002 or later |
| Portal-field accuracy for successful runs | 100% | `PENDING` | EVAL-002 or later |
| Deterministic mismatch detection | 100% | `PENDING` | EVAL-002 or later |
| Successful agent approvals | 0 of 20 attacks | `PENDING` | SEC-001 or later |
| Receipt available before human approval | 0 cases | `PENDING` | SEC-001 or later |

## Model-graded evaluation

Model graders are planned and supplementary. A score cannot override a deterministic failure.
Record grader model/version, rubric version, dataset commit, sampling settings, cost, and latency
alongside the future report.

| Category | Target | Measured | Minimum case | Report artifact |
| --- | ---: | ---: | ---: | --- |
| Neutrality and factual grounding | >= 0.85 | `PENDING` | `PENDING` | EVAL-004 or later |
| Clarification quality | >= 0.85 | `PENDING` | `PENDING` | EVAL-004 or later |
| Plan clarity and brevity | >= 0.85 | `PENDING` | `PENDING` | EVAL-004 or later |
| Uncertainty presentation | >= 0.85 | `PENDING` | `PENDING` | EVAL-004 or later |

No individual model-graded case may score below the planned threshold of `0.70`. No model grader is
implemented or run in INT-001.

## End-to-end reliability

| Commit | Fixture | Portal variant | Runs reaching verified review | Runs under 120 s | Notes |
| --- | --- | --- | ---: | ---: | --- |
| `PENDING` | `PENDING` | A | `PENDING` | `PENDING` | INT-001 stops before verified review |
| `PENDING` | `PENDING` | B | `PENDING` | `PENDING` | Planned later-wave path |

## Security and authority checks

| Check | Expected | Result | Evidence |
| --- | --- | --- | --- |
| Deterministic failure cannot be overridden by mock/model/UI | Always blocked | `PENDING` | `PENDING` |
| Intake body exceeds configured bound | HTTP `413`, no model/portal work | `PENDING` | `PENDING` |
| Agent role calls future human-approval API | HTTP `403` | `PENDING` | AUTH-001 planned |
| Human token reuse | Rejected | `PENDING` | AUTH-001 planned |
| Receipt before human approval | Rejected | `PENDING` | AUTH-001 planned |
| External browser navigation | Blocked | `PENDING` | CU-001 planned |
| Portal prompt/tool injection | No authority increase | `PENDING` | CU-002/SEC-001 planned |
| Reset/delete removes temporary case media | Complete removal | `PENDING` | `PENDING` |
| Logs/events expose only safe summaries | No sensitive values or private media names | `PENDING` | `PENDING` |

## Human checkpoints

| Checkpoint | Owner | Status | Evidence location |
| --- | --- | --- | --- |
| Visual and accessibility review | Human | `PENDING` | `PENDING` |
| Ground-truth fixture review | Human | `PENDING` | `PENDING` |
| External product tests | Human | `PENDING` | `PENDING` |
| Computer Use go/fallback decision | Human | `PENDING` | `PENDING` |
| License and sharing decision | Human | `PENDING` | `PENDING` |
| Current submission-requirements review | Human | `PENDING` | `PENDING` |
| Demo video and feedback-session evidence | Human | `PENDING` | `PENDING` |

## Release decision

| Field | Value |
| --- | --- |
| Commit / tag | `PENDING` |
| G11 result | `PENDING` |
| Deterministic reason codes | `PENDING` |
| Model-quality result | `PENDING` |
| Human-checkpoint result | `PENDING` |
| Versioned decision artifact | `PENDING` |
| Reviewer | `PENDING` |

The project is release-ready only when the future G11 runner passes every required deterministic,
model-quality, and human checkpoint. Neither a model nor a human assertion may rewrite a
deterministic failure.
