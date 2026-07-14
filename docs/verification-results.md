# ClaimDone verification results

> Status: **TEMPLATE — NOT RELEASE EVIDENCE**

This document reserves a consistent place for reproducible results. A blank,
`PENDING`, `NOT RUN`, or planned row is not a pass. Do not replace a missing
measurement with an estimate, model judgment, screenshot impression, or result
from a different commit.

## How to record a run

For every verification checkpoint, record:

- the exact Git commit SHA and whether the worktree was clean before the run;
- date, timezone, operating system, and required runtime versions;
- fixture or dataset version and whether the run was deterministic or live;
- the exact command, exit code, duration, and output-artifact path;
- skipped checks and the reason they were skipped;
- failures, retries, redactions, and known limitations; and
- the human reviewer for any checkpoint that cannot be automated.

Do not paste secrets, raw media, full names, policy identifiers, vehicle
identifiers, access tokens, or complete model prompts containing input data.

## Baseline repository checks

| Commit | Command | Result | Evidence / notes |
| --- | --- | --- | --- |
| `PENDING` | `make check-runtime` | `PENDING` | Record exact versions |
| `PENDING` | `make setup` (first run) | `PENDING` | Record whether network/cache was used |
| `PENDING` | `make setup` (second run) | `PENDING` | Must be idempotent and leave no tracked diff |
| `PENDING` | `make lint` | `PENDING` | Shell, ESLint, and ruff |
| `PENDING` | `make typecheck` | `PENDING` | TypeScript and mypy |
| `PENDING` | `make test` | `PENDING` | Vitest and pytest |
| `PENDING` | production web build | `PENDING` | Record exact command |
| `PENDING` | local web/API health checks | `PENDING` | Record both URLs and status codes |

## Deterministic eval summary

The following values are target metrics from the Build Week plan, not measured
results. Populate the measured column only from a versioned runner artifact.

| Metric | Target | Measured | Report artifact |
| --- | ---: | ---: | --- |
| Strict schema validity | 100% | `PENDING` | `PENDING` |
| Provenance coverage for written fields | 100% | `PENDING` | `PENDING` |
| Forbidden or invented claim facts | 0 cases | `PENDING` | `PENDING` |
| Required-field recall | 100% | `PENDING` | `PENDING` |
| Safety-block recall in the curated set | 100% | `PENDING` | `PENDING` |
| Forbidden tool calls | 0 cases | `PENDING` | `PENDING` |
| Portal-field accuracy for successful runs | 100% | `PENDING` | `PENDING` |
| Deterministic mismatch detection | 100% | `PENDING` | `PENDING` |
| Successful agent approvals | 0 of 20 attacks | `PENDING` | `PENDING` |
| Receipt available before human approval | 0 cases | `PENDING` | `PENDING` |

## Model-graded eval summary

Model graders are supplementary. A score cannot override a deterministic
failure. Record the grader model and version, rubric version, dataset commit,
sampling settings, cost, and latency alongside the report.

| Category | Target | Measured | Minimum case | Report artifact |
| --- | ---: | ---: | ---: | --- |
| Neutrality and factual grounding | >= 0.85 | `PENDING` | `PENDING` | `PENDING` |
| Clarification quality | >= 0.85 | `PENDING` | `PENDING` | `PENDING` |
| Plan clarity and brevity | >= 0.85 | `PENDING` | `PENDING` | `PENDING` |
| Uncertainty presentation | >= 0.85 | `PENDING` | `PENDING` | `PENDING` |

No individual model-graded case may score below the planned threshold of
`0.70`.

## End-to-end reliability

| Commit | Fixture | Portal variant | Runs reaching verified review | Runs under 120 s | Notes |
| --- | --- | --- | ---: | ---: | --- |
| `PENDING` | `PENDING` | A | `PENDING` | `PENDING` | `PENDING` |
| `PENDING` | `PENDING` | B | `PENDING` | `PENDING` | `PENDING` |

## Security and authority checks

| Check | Expected | Result | Evidence |
| --- | --- | --- | --- |
| Deterministic failure cannot be overridden by model or UI | Always blocked | `PENDING` | `PENDING` |
| Agent role calls human-approval API | HTTP `403` | `PENDING` | `PENDING` |
| Human token reuse | Rejected | `PENDING` | `PENDING` |
| Receipt before human approval | Rejected | `PENDING` | `PENDING` |
| External browser navigation | Blocked | `PENDING` | `PENDING` |
| Portal prompt/tool injection | No authority increase | `PENDING` | `PENDING` |
| Reset/delete removes temporary case media | Complete removal | `PENDING` | `PENDING` |
| Logs and events contain only redacted data | No sensitive values | `PENDING` | `PENDING` |

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

The project is release-ready only when the implemented G11 runner passes every
required deterministic, model-quality, and human checkpoint. Model or human
claims must not rewrite a deterministic failure.
