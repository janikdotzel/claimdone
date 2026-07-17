# ClaimDone

ClaimDone is a deliberately minimal hackathon demo. A user adds one to three synthetic accident photos and a short text or voice description. AI either asks for exactly one missing detail or prepares an editable insurance-claim preview. Computer Use can then fill a local insurer-portal sandbox for review.

This project never submits a real insurance claim. Use synthetic data only.

Build Week logistics, the demo outline, and the remaining submission checklist live in [`HACKATHON_SUBMISSION.md`](HACKATHON_SUBMISSION.md). This README is the canonical guide to the current app.

## What the demo does

1. Add 1–3 JPG or PNG accident photos.
2. Describe what happened with text or a voice memo.
3. Analyze the evidence with AI.
4. Answer at most one missing-information question.
5. Review and edit the generated claim.
6. Let Computer Use navigate the local synthetic portal and fill its incident form.
7. Review the final sandbox screenshot. Nothing is submitted.

## Requirements

- Node.js `24.14.0`
- npm `11.9.0`
- Google Chrome installed locally for the Computer Use sandbox
- An OpenAI API key
- Internet access for live OpenAI requests

`playwright-core` controls an existing local Chrome installation; it does not download a browser.

## Quick start

```bash
git clone https://github.com/janikdotzel/claimdone.git
cd claimdone
nvm install
npm ci
cp .env.example .env.local
```

Add your API key to `.env.local`:

```dotenv
OPENAI_API_KEY=your_key_here
```

Then start the app:

```bash
npm run dev
```

Open [http://127.0.0.1:3001](http://127.0.0.1:3001) for the customer view or
[http://127.0.0.1:3001/demo](http://127.0.0.1:3001/demo) for the presenter view.

The presenter view shows validated evidence checks and replays screenshots captured
during the real Computer Use run. To additionally show the isolated Chrome window
while Computer Use operates, set this local-only option in `.env.local` and restart
the app:

```dotenv
CLAIMDONE_SHOW_COMPUTER_USE_BROWSER=true
```

The visible window is a separate isolated Chrome process, not the user’s personal
browser profile. The captured replay remains available either way.

The port is intentionally fixed. The Computer Use safety boundary only permits the three exact local sandbox paths under `http://127.0.0.1:3001/portal/sandbox`, so changing the dev or start port will break the portal handoff.

## Bundled sample data

Both customer and presenter views start with a synthetic description and three synthetic accident photos, so a judge can run the demo immediately. The images live in [`public/images/claim-flow`](public/images/claim-flow):

- `accident-overview.jpg`
- `accident-damage.jpg`
- `accident-context.jpg`

Replacing the sample photos through the upload control keeps the same 1–3 photo flow. Do not use real accident or customer data.

## Verify the demo

```bash
npm run lint
npm run typecheck
npm test
npm run build
```

The automated tests use controlled provider and browser doubles. They do not need an API key, make live OpenAI requests, or launch Chrome. A complete live run requires the configured key, network access, Chrome, and the app running on port `3001`.

To run the production build locally:

```bash
npm run build
npm run start
```

## Quick judge walkthrough

1. Open `/demo`; the synthetic evidence is already staged and the activity history is intentionally empty.
2. Select **Analyze accident**. The presenter panel shows the validated photo and statement review, then identifies the missing date and time.
3. Add a date and time and save the claim. The activity panel records the customer correction and the completed decision.
4. Select **Fill insurer portal sandbox**. Computer Use opens the local Demo Mutual home page, follows the two permitted links, fills the five approved fields, and stops before submission.
5. Review the captured browser replay and final sandbox result. No real insurer is contacted and no claim is submitted.

## Routes and models

- `/` contains the complete four-state flow: input, analyzing, needs information, and ready.
- `/demo` runs the same flow with presenter-only agent activity and Computer Use replay.
- `POST /api/analyze` validates the evidence and returns either a ready claim or one question.
- `POST /api/demo/analyze` returns the same result plus validated observable activity.
- `POST /api/portal-handoff` runs the restricted Computer Use loop.
- `POST /api/demo/portal-handoff` returns the verified result plus captured replay frames.
- `/portal/sandbox` is the synthetic portal home page.
- `/portal/sandbox/claims` is the synthetic claims overview.
- `/portal/sandbox/claims/new` contains the five-field incident form.
- `/portal` displays the final sandbox screenshot held in browser memory.

The demo uses `gpt-5.6` for the core image-analysis and claim-preparation path, `gpt-5.4-mini` for Computer Use, and `gpt-4o-mini-transcribe` for voice memos. Provider output is validated with strict Zod schemas before it reaches the UI.

## How Codex shaped the build

Codex was used to turn a production-oriented prototype into this focused demo, implement and test the complete flow, review the responsive UI in a browser, and harden the local Computer Use boundary. The main decisions were:

- one standalone Next.js app instead of a multi-service architecture;
- four customer-facing states with at most one follow-up question;
- a separate `/demo` presenter lens that reveals validated agent activity without complicating the customer view;
- a local, synthetic insurer portal with an allowlisted navigation and form-filling policy;
- memory-only state, synthetic evidence, and a hard stop before submission.

For a clear before-and-after record, commit `4c3fb1b` snapshots the legacy implementation, commit `ec14e06` introduces the minimal demo, and pull request #1 merges it into `main`.

## Data and safety boundaries

- The API key is server-only and `.env.local` is ignored by Git.
- Photos, audio, claim data, and screenshots are not stored in a database by this app.
- Client state and the prepared screenshot exist in memory only; a reload resets the flow.
- Evidence sent for live analysis is processed through the configured OpenAI API. Use synthetic inputs only.
- Computer Use is restricted to three exact local paths, two ordered navigation links, and five known fields.
- Only `View claims` followed by `Start a motor claim` may be clicked. All other links, buttons, downloads, arbitrary navigation, file uploads, and submit actions are blocked.
- The server verifies every filled field against the reviewed claim before reporting success.
- Safety checks, unexpected actions, timeouts, and turn or action limits stop the handoff.
- The sandbox contains no submit control and cannot send data to an insurer.

## Input limits

- Photos: 1–3 files, JPG or PNG, up to 8 MB each.
- Text: 1–1,500 characters.
- Voice memo: M4A, MP3, WAV, or WebM, up to 60 seconds and 10 MB.
- Missing information: at most one follow-up question.

## Deliberate non-goals

- No real insurer integration, real portal automation, or claim submission
- No arbitrary portal URL
- No authentication, user accounts, dashboard, sidebar, or KPI surfaces
- No database, persistence, queues, background jobs, SSE, or WebSockets
- No separate backend, FastAPI service, or generated cross-runtime contracts
- No custom audio recorder
- No upload of photo files into the insurer sandbox; the portal receives only the attachment count
- No deployment configuration or production hosting assumptions

This is a local hackathon demonstration, not a production insurance system.
