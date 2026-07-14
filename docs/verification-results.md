# ClaimDone verification results

> Status: **INT-001 LOCALLY VERIFIED — NOT RELEASE EVIDENCE**

The deterministic no-live-AI walking skeleton was integrated and verified at commit
`ae2763bff760114a82bfb23620bcf4d01723466e`. Canonical checks, the production web build, live local
services, text and synthetic-WAV flows, cleanup, repository reset, and restart passed at that
anchor. A blank, `PENDING`, `NOT RUN`, planned row, or expectation is still not a pass, and this
scoped INT-001 record must not be presented as release approval or a product-quality benchmark.

## Recorded run identity

| Field | Recorded value |
| --- | --- |
| Integration anchor | `ae2763bff760114a82bfb23620bcf4d01723466e` |
| Date / timezone | 2026-07-14 / Europe/Berlin |
| Operating system | Darwin |
| Runtime | Node.js 24.14.0; pnpm 11.7.0; Python 3.12.13; uv 0.8.3 |
| Input boundary | Staged synthetic inputs only; text and hash-fixed synthetic PCM WAV |
| External services | No OpenAI, transcription, browser-runner, or other live-AI call |
| Evidence type | Canonical command output and direct HTTP observations against both local services |
| Known record gap | Exact wall-clock durations and a versioned report artifact were not captured |

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

The recorded evidence supports these scoped statements:

- the Welle-1 case/persistence, media, G0-G5, frontend intake, portal, contract, and INT-001 workflow
  implementations are composed in one local integration;
- EVAL-001 contains exactly twelve static synthetic cases and its validator is included in the
  passing canonical lint, typecheck, and test runs;
- the deterministic mock flow asks exactly one clarification and rejects stale and duplicate
  answers without portal mutation; and
- both statement modes reached backend `verifying`, portal `review`, and verification `pending`,
  with exact rendered-value comparison and no private media storage handle in the API response.

This does not measure EVAL-001 as a product benchmark, exercise a live model, implement independent
G8 verification, approve or submit a claim, or satisfy the future G11 release gate.

## INT-001 integration checklist

Every result in this table refers to the single integration anchor named above. Component-branch
results were not substituted for integrated measurements.

| Commit | Check | Expected | Result | Evidence / notes |
| --- | --- | --- | --- | --- |
| `ae2763b` | Worktree at measurement anchor | Focused INT-001 integration | `PASS` | Canonical verification ran from the named anchor; later documentation edits are not part of that measurement |
| `ae2763b` | `make check-runtime` | Exact pinned runtimes available | `PASS` | Node.js 24.14.0, pnpm 11.7.0, Python 3.12.13, uv 0.8.3 |
| `ae2763b` | `make setup` (first successful run) | Frozen environments install | `PASS` | Installed one previously unavailable package after approved network access |
| `ae2763b` | `make setup` (second run) | Idempotent and no tracked diff | `PASS` | Audited 27 packages; produced no tracked diff |
| `ae2763b` | `make lint` | Shell, ESLint, ruff, including `evals/` | `PASS` | All configured lint stages passed |
| `ae2763b` | `make typecheck` | Strict TypeScript and mypy, including `evals/` | `PASS` | Next.js/TypeScript checks passed; mypy checked 64 Python source files |
| `ae2763b` | `make test` | Vitest and all configured pytest paths pass | `PASS` | 87 Vitest tests and 264 pytest tests passed, including `evals/tests` |
| `ae2763b` | Pinned-runtime `pnpm build:web` equivalent | Production Next.js build passes | `PASS` | Portable runtime resolver selected the pinned Node.js and pnpm versions; Next.js production build passed |
| `ae2763b` | `make dev`, both `/health` routes, and `/claim/new` | Both services healthy and product route served | `PASS` | Web health `200`; API health `200`; `/claim/new` `200` |
| `ae2763b` | `make reset` twice with services stopped | Generated state removed; protected files preserved; repeat is idempotent | `PASS` | First run removed 22 generated entries; second removed 0; environment files, dependencies, sources, fixtures, and tools were preserved |
| `ae2763b` | Restart after reset | Fresh local state without manual DB edits | `PASS` | Both services restarted and the complete text plus synthetic-WAV walkthrough passed again |

The dependency lock at the anchor resolves `python-multipart==0.0.32` and production
`httpx==0.28.1`. No machine-specific binary path is recorded or committed.

## INT-001 no-live-AI walkthrough

Use staged synthetic media only. The flow must not require `OPENAI_API_KEY`, an external network
service, a transcription API, or a browser runner.

| Step | Expected deterministic observation | Result | Evidence / notes |
| --- | --- | --- | --- |
| Open `/claim/new` | Product route loads; no case can advance on UI assertion alone | `PASS` (HTTP scope) | Route returned `200`; visual/accessibility approval was not part of this direct-HTTP run |
| Submit invalid intake | Server G0 or G1 failure blocks mock extraction and portal calls | `PASS` | Missing consent produced G0 HTTP `422`; no portal mutation was observed |
| Submit valid multipart intake | Positive expected version, exactly 3 JPG/PNG images, text XOR PCM WAV, 3 consents, 3 EXIF decisions accepted | `PASS` | Both text and hash-fixed synthetic-WAV modes passed before and after reset |
| Receive clarification | Exactly one structured, version-bound question | `PASS` | Exactly one clarification was observed for each valid path |
| Submit stale clarification | HTTP conflict boundary; no portal mutation | `PASS` | HTTP `409`; portal snapshot remained unchanged |
| Submit valid clarification once | Packet rebuilt and authoritative G2-G5 path reruns | `PASS` | The valid version-bound answer advanced both statement modes |
| Portal fill | Server-provided loopback link opens `/sandbox/A/cases/{caseId}` | `PASS` | Variant A was filled; returned rendered values matched the expected draft values exactly |
| Final INT-001 boundary | Backend `verifying`; portal `review`; verification `pending` | `PASS` | All three states were observed together for text and audio paths |
| Duplicate clarification | Rejected; no second round or duplicate portal mutation | `PASS` | HTTP `409`; portal snapshot remained unchanged |
| API privacy boundary | No private media storage name in public responses | `PASS` | No storage handle leak was observed in the HTTP walkthrough |
| Case and portal delete | Associated media, case rows, and portal case removed | `PASS` | Backend case/media cleanup and portal deletion both succeeded |
| Backend developer reset | ClaimDone-owned cases, mapped media, and recognized orphans removed | `PASS` | Reported 0 remaining entries after case-scoped cleanup |
| Portal fixture reset | Portal state returns to its selected fixture | `PASS` | Separate portal reset reported 0 remaining entries after cleanup |

The HTTP walkthrough ran once before and once after repository reset; each round covered text and
synthetic-WAV input. The second round recorded these redacted-safe correlation IDs:

| Path | Intake request ID | Review request ID |
| --- | --- | --- |
| Text | `request-c416e6c8ed7f4f9d95f56aa83e941760` | `request-49e0546332744800ba0403fa96bde7cc` |
| Synthetic WAV | `request-96fdc9277dbc4a65b59d8cc3aae2c16a` | `request-976b026b0e36493d9000b2ba8f2798a4` |

## EVAL-001 structural dataset

EVAL-001 is a static expected-case dataset, not an executed product benchmark. Its validator
requires exact contract validity, unique IDs, exactly twelve cases, required category coverage,
synthetic fixture IDs, explicit safety reason codes, and no expected tool execution after a
deterministic pre-tool G3 block.

| Commit | Command | Expected | Result | Evidence / notes |
| --- | --- | --- | --- | --- |
| `ae2763b` | `make lint` | Validator and eval tests pass ruff | `PASS` | Included in the integrated canonical lint run |
| `ae2763b` | `make typecheck` | Validator and eval tests pass strict mypy | `PASS` | Included in the integrated 64-source-file mypy run |
| `ae2763b` | `make test` | Dataset tests discover exactly 12 cases without live services | `PASS` | Included in the 264 passing pytest tests; no live service or model invoked by EVAL-001 |

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

| Commit | Fixture | Portal variant | Runs reaching the INT-001 boundary | Runs under 120 s | Notes |
| --- | --- | --- | ---: | ---: | --- |
| `ae2763b` | Synthetic text plus hash-fixed synthetic WAV | A | 2 complete rounds | `NOT RECORDED` | One round before and one after reset; each covered both input modes; INT-001 stops before independent G8 verification |
| `PENDING` | `PENDING` | B | `PENDING` | `PENDING` | Planned later-wave path |

Two successful local rounds are integration evidence, not a statistically meaningful reliability
rate. No latency claim is made because exact durations were not captured.

## Security and authority checks

| Check | Expected | Result | Evidence |
| --- | --- | --- | --- |
| Deterministic failure cannot be overridden by mock/model/UI | Always blocked | `PASS` (INT-001 scope) | Live G0 consent failure returned `422`; negative gate paths are included in the passing automated suite |
| Intake body exceeds configured bound | HTTP `413`, no model/portal work | `PASS` (automated) | Covered by the passing pytest suite; not repeated as a live HTTP measurement |
| Agent role calls future human-approval API | HTTP `403` | `PENDING` | AUTH-001 planned |
| Human token reuse | Rejected | `PENDING` | AUTH-001 planned |
| Receipt before human approval | Rejected | `PENDING` | AUTH-001 planned |
| External browser navigation | Blocked | `PENDING` | CU-001 planned |
| Portal prompt/tool injection | No authority increase | `PENDING` | CU-002/SEC-001 planned |
| Reset/delete removes temporary case media | Complete removal | `PASS` | Live case/media and portal deletion succeeded; both app resets reported 0 after cleanup |
| Logs/events expose only safe summaries | No sensitive values or private media names | `PARTIAL` | No storage handle appeared in the observed API responses; comprehensive observability review remains pending |

## Human checkpoints

| Checkpoint | Owner | Status | Evidence location |
| --- | --- | --- | --- |
| Visual direction | Human | `APPROVED` | User approved the code-first UX direction |
| Figma artifact for V1 | Product decision | `N/A` | V1 uses code-first tokens, components, states, and accessibility implementation; Figma is not required |
| Complete accessibility review | Human | `PENDING` | Later checkpoint; not implied by visual-direction approval |
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
