# ClaimDone Build Week submission

This document is the single source of truth for hackathon logistics and the demo narrative. Product setup and technical behavior are documented in [`README.md`](README.md).

Last reviewed: July 17, 2026.

## Product story

ClaimDone turns one to three accident photos and a short written or spoken description into an editable insurance-claim preview. If one essential fact is missing, the agent asks exactly one question. Once the user approves the claim, Computer Use navigates a synthetic insurer portal, fills the approved fields, verifies them, and stops before submission.

The two demo moments are:

1. **Visible agent reasoning:** `/demo` exposes a concise, validated activity ledger for each photo, the customer statement, completeness checks, corrections, and the final decision.
2. **Visible Computer Use:** the presenter view replays screenshots captured from the actual isolated browser run, including portal navigation, field entry, and final verification.

The customer view remains deliberately simple. The presenter lens explains what happened behind the scenes without exposing chain-of-thought or technical logs.

## Recommended demo outline

Aim for about 2 minutes and 15 seconds; the submitted video must stay under three minutes and include narration.

1. **Problem, 0:00–0:15** — Accident reporting is stressful and insurer forms are repetitive.
2. **Evidence, 0:15–0:35** — Show the three synthetic photos and short description already staged on `/demo`.
3. **Agent review, 0:35–0:55** — Run the analysis. Point out that the agent checks each source and asks only for the missing date and time.
4. **Human correction, 0:55–1:10** — Add the missing detail and show the activity ledger update to a complete decision.
5. **Computer Use, 1:10–1:45** — Start the portal handoff. Show the isolated browser or captured replay navigating from the insurer home page to the form and filling the reviewed values.
6. **Safety, 1:45–2:00** — Emphasize that the sandbox has no submit control, the agent is path- and action-restricted, and nothing reaches a real insurer.
7. **Built with Codex, 2:00–2:15** — Briefly show how Codex helped replace the legacy prototype, iterate on the design, test the states, and implement the Computer Use boundary.

The closing line can be: “Three photos, one short description, and a reviewed claim ready for the insurer portal—without hiding control from the user.”

## Build provenance and key decisions

This repository contains a clear before-and-after history for the pre-existing project:

- `4c3fb1b` — snapshot of the legacy production-oriented ClaimDone implementation;
- `ec14e06` — replacement with the standalone minimal hackathon demo;
- pull request #1 / `dc652f4` — reviewed merge into `main`.

Codex was used for product simplification, implementation, visual browser review, tests, safety debugging, documentation, and repository integration. The current design intentionally uses:

- one Next.js application and one local synthetic portal;
- a simple customer flow plus an optional presenter-only transparency layer;
- strict Zod validation around provider output;
- in-memory data only;
- deterministic restrictions around Computer Use;
- no real submission, insurer integration, or customer data.

## Official submission requirements

The official deadline is **July 21, 2026 at 5:00 PM PDT**, which is **July 22 at 02:00 CEST**.

Before submitting:

- [ ] Select one eligible track in Devpost. “Apps for Your Life” is the likely fit, but the final choice is still open.
- [x] Use GPT-5.6 for the app’s meaningful image-analysis and claim-preparation path.
- [ ] Record a public YouTube video under three minutes with audio or voiceover.
- [ ] Show the working product and explain how Codex and GPT-5.6 were used.
- [ ] Write the Devpost project description in English.
- [ ] Add the repository URL.
- [ ] Provide judges with free, unrestricted access to a working website, functioning demo, or test build, including all required testing instructions.
- [ ] If the repository is public, add a relevant open-source license. If it remains private, share it with `testing@devpost.com` and `build-week-event@openai.com`.
- [ ] Confirm that the video, screenshots, music, logos, and other submission assets are original or properly licensed and do not misuse third-party trademarks.
- [ ] Run the setup and judge walkthrough from a clean checkout.
- [ ] Submit the primary build task’s Codex Session ID through `/feedback` and record it in the private submission notes.
- [ ] Recheck the final entry against the official rules immediately before submission.

Already covered in the repository:

- [x] Reproducible npm setup and verification commands.
- [x] Bundled synthetic sample data and a documented judge path.
- [x] Codex usage, build decisions, and before-and-after provenance.
- [x] All customer-facing product copy is English.
- [x] No real insurer submission or identifying data.

Official sources:

- [OpenAI Build Week rules](https://openai.devpost.com/rules)
- [OpenAI Build Week FAQ](https://openai.devpost.com/details/faqs)
