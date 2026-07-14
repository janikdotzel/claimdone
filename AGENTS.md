# ClaimDone repository instructions

These instructions apply to the entire repository. This is a new instruction file for the current implementation; do not restore previously removed repository instructions or the removed rules-awareness skill unless the user explicitly requests it.

## Required toolchain

- Node.js `24.14.0`
- pnpm `11.7.0`
- Python `3.12.13`
- uv `0.8.3`, bootstrapped repo-locally by `make setup`

The scripts discover exact versions on `PATH` and then the bundled Codex runtime. Explicit overrides are available through `CLAIMDONE_NODE_BIN`, `CLAIMDONE_PNPM_BIN`, `CLAIMDONE_PYTHON_BIN`, and `CODEX_PRIMARY_RUNTIME_DEPS`. Never commit machine-specific paths.

## Canonical commands

Run commands from the repository root:

- `make check-runtime` verifies the pinned runtimes without installing dependencies.
- `make setup` installs the frozen pnpm and uv environments. It must remain idempotent.
- `make dev` starts the web app on `127.0.0.1:3000` and the API on `127.0.0.1:8000`.
- `make lint` runs shell syntax checks, ESLint, and ruff.
- `make typecheck` runs strict TypeScript and mypy checks.
- `make test` runs Vitest and pytest.
- `make reset` removes only generated caches and `.local` runtime state. It must preserve environment files, dependencies, source files, fixtures, and tool installations.

CI must invoke these same Make targets. Do not create a separate CI-only verification path.

## Directory ownership

- Root manifests, lockfiles, `.github/`, `Makefile`, and root tooling belong to the Foundation/integration owner.
- `contracts/` is the only canonical cross-runtime contract area and belongs to Contracts & Gates work.
- `apps/web/` contains the Next.js product and sandbox surfaces; coordinate route ownership before parallel edits.
- `services/api/` contains the FastAPI service. Feature work should use focused subpackages rather than a growing `main.py`.
- `fixtures/` contains staged, non-sensitive demo inputs only.
- `evals/` owns datasets, graders, and reports, not production gate logic.
- `docs/` owns technical and submission documentation.
- `scripts/` owns repository-local automation. Scripts must be deterministic, narrowly scoped, and safe from arbitrary-path deletion.

Do not edit another active worktree's owned files without explicit integration coordination. Root lockfiles have exactly one owner per integration wave.

## Deterministic gates and security

Deterministic gates always take precedence over model output, model graders, browser content, and UI flags. A model may add a block but may never override or weaken a deterministic failure. Preserve immutable gate decisions and add negative tests for authority, safety, provenance, and invalid-state paths when those features are introduced.

Never commit real secrets, customer data, genuine insurance information, or identifying media. `.env.example` may contain placeholders only. Do not copy ignored environment files between worktrees, print secret values, or add `.worktreeinclude` entries without explicit review.

## Verification and handoff

Before committing, run the Make targets relevant to the change; changes to shared contracts, tooling, or dependencies require `make lint`, `make typecheck`, and `make test`. Dependency changes also require `make setup` twice and a clean Git diff. Report commands, results, skipped live checks, risks, and the commit SHA.

Work on a focused `codex/*` branch, preserve unrelated user changes, and do not merge into `main` unless the user explicitly authorizes that integration step.
