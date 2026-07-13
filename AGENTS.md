# ClaimDone project instructions

These instructions apply to the entire repository.

## Devpost Hackathons plugin

Use the **Devpost Hackathons** plugin as the primary source for current OpenAI Build Week information and for Devpost project operations. Prefer it over scraping Devpost pages or relying on dates, rules, tracks, or requirements copied into local files, because the live challenge data can change.

Known identifiers:

- Hackathon slug: `openai`
- Hackathon: OpenAI Build Week
- Devpost project ID: `1326862`
- Devpost project slug: `claimdone`
- Project name: ClaimDone

Treat the identifiers as opaque values and verify them with `list_hackathons` or `list_my_projects` if a lookup fails.

### Installed Build Week skill

The repository includes [`skills/build-week-rules-awareness/SKILL.md`](skills/build-week-rules-awareness/SKILL.md). Use it whenever the user asks for a compliance review, deadline audit, rules-awareness check, or the scheduled nightly rules report. Read that skill in full before starting the review and follow its required evidence sources, four finding labels, deadline-warning cadence, and idempotent report path exactly.

The skill is deliberately read-only with respect to Devpost and the product: it may inspect evidence and write its prescribed report, but it must not submit, edit Devpost fields, publish, spend money, request credits, or modify implementation files. The Devpost plugin is the preferred live source for announcements, dates, submission state, and official challenge content during that review.

### Read workflow

Use the narrowest relevant plugin operation:

1. Call `get_hackathon_overview` first when grounding a general answer about Build Week.
2. Call `get_announcements` before answering anything about recent changes, deadline shifts, or new host guidance.
3. Call `get_hackathon_rules` for eligibility, intellectual property, permitted work, team constraints, or formal requirements. Quote formal rule and eligibility language verbatim when precision matters; do not paraphrase it into a stronger or weaker claim.
4. Call `get_key_dates` for deadlines and the current phase. Convert UTC timestamps to `Europe/Berlin` for the user and state CEST/CET explicitly, while retaining the source timestamp.
5. Call `get_judging_criteria` before recommending a track, prioritizing features for judging, or reviewing submission strength.
6. Call `get_submission_requirements` before drafting a final submission checklist or collecting submission answers.
7. Use `list_my_projects` and then `get_project` to inspect the live ClaimDone project. Do not assume local Markdown matches the current Devpost draft.

Do not use browser automation for data or actions the plugin exposes directly.

If plugin endpoints, copied rules, or local summaries disagree, do not silently choose one. Re-fetch the formal rules and announcements, treat official rules and official notices as controlling, and record the discrepancy with the source and retrieval time. Never weaken a formal requirement based only on a summary or date endpoint.

### Current ClaimDone submission direction

ClaimDone fits **Apps for Your Life**, the consumer-app track that includes everyday personal finance. Re-check the available tracks with `get_judging_criteria` or `get_submission_requirements` before final submission.

The live submission requirements currently include:

- A working project built with Codex using GPT-5.6.
- One selected category.
- A project description.
- A public YouTube demo video under three minutes, with voiceover explaining the project and how both Codex and GPT-5.6 were used.
- A public repository with an appropriate license, or a private repository shared with the judging addresses specified by Devpost.
- A README with setup instructions, sample data where needed, and clear run guidance.
- A description of where Codex accelerated development, where key decisions were made, and how Codex and GPT-5.6 were used.
- The `/feedback` Codex Session ID for the session where most core functionality was built.

Re-fetch these requirements before treating the list as final.

### Mutation workflow and safety

Devpost changes are external, user-visible actions. Read operations are safe by default. Create, update, upload, invite, register, and submit operations require a direct user request that clearly authorizes that action.

- Before `register_for_hackathon`, always call `get_registration_form`, collect every required answer, and obey `can_register` and `blocked_reason`.
- Before `update_project`, call `get_project`. Remember that `links` and `built_with` replace their existing values rather than merging with them. Do not update Devpost merely because a local Markdown file changed.
- Before `submit_project`, always call `get_submission_requirements`, verify the project is complete, collect every required custom answer, and ensure the user explicitly asked to submit or resubmit. Drafting or editing is not submission authorization.
- For the project thumbnail, use `prepare_thumbnail_upload` for normal project images larger than about 50 KB. Follow its out-of-band upload command so image bytes do not enter the model context. The current `CLAIMDONE_THUMBNAIL.png` is about 1.9 MB and belongs on this path, not the small base64 upload path.
- Treat a team invite link as a secret. Only fetch it when the user asks, and explain that joining makes the person a project member across every hackathon entry for that project.
- After any mutation, verify the authoritative success response once. Do not retry a successful upload, update, or submission.

### Recommended checkpoints

- At the start of a build day: check `get_announcements` and `get_key_dates`.
- Before choosing or revisiting scope: check `get_judging_criteria`.
- Before recording the final demo: check `get_submission_requirements` and announcements.
- Before editing the Devpost story: call `get_project`, draft changes locally, then update only after explicit approval.
- Before final submission: run the complete mandatory `get_submission_requirements` → completeness review → required answers → explicit submit workflow.
