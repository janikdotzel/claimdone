---
name: build-week-rules-awareness
description: Nightly deadline and rules-awareness check for OpenAI Build Week 2026.
---

# Build Week Rules Awareness

Run a read-only, evidence-based compliance and deadline review for the Build Week project in `/Users/janikdotzel/Documents/build-week`.

## Authority and limits

- Treat the official rules supplied by Janik at `/Users/janikdotzel/.codex/attachments/f73fe340-a540-4c8a-a2c5-ca4f29c5aa1f/pasted-text.txt` as the baseline source.
- When internet access is available, check `openai.devpost.com` and the official rules page for amendments or notices. Official rules and official notices override summaries and AI output.
- This is an awareness check, not legal advice. Never claim a violation without direct evidence. Use exactly these labels: `CONFIRMED ISSUE`, `POSSIBLE RISK`, `UPCOMING DEADLINE`, and `CLEAR`.
- Do not submit the entry, alter Devpost fields, publish content, spend money, request credits, or change project files. Read-only inspection only.
- Do not expose secrets, private credentials, or unrelated personal information in the report.

## Authoritative deadlines

All local conversions below are Europe/Berlin (CEST) for 2026:

- Optional $100 credit request: July 17 at 21:00 CEST (July 17 at 12:00 PT); credits are subject to approval/availability and must be used by July 31.
- Registration closes: July 22 at 02:00 CEST (July 21 at 17:00 PT).
- Submission closes and becomes uneditable: July 22 at 02:00 CEST (July 21 at 17:00 PT).
- Judging ends: August 6 at 02:00 CEST (August 5 at 17:00 PT). The working project and free, unrestricted judge access must remain available until then.
- Winners are expected around August 12 at 23:00 CEST (August 12 at 14:00 PT).
- If winner forms are received, they must be returned within 10 business days after they are sent.

For every incomplete deadline, warn at 7 days, 3 days, 48 hours, 24 hours, 12 hours, and on the local calendar day. If a deadline is closer than the next run, say so prominently and include the exact time remaining.

## Evidence to inspect

Read the workspace, git history/status, `BUILD_WEEK_SPEC.md`, `BUILD_WEEK_DIARY.md`, README and submission assets when present. Inspect the Devpost draft and official notices only when access is available without bypassing sign-in, CAPTCHA, MFA, or consent. A missing artifact is a confirmed issue only if its absence can be verified in the actual submission state; otherwise label it a possible risk.

## Compliance checklist

Check and report on:

1. Eligibility and entry: eligible location/age; registration completed; authorized representative appointed for a team or organization; no excluded affiliation, judge relationship, sponsor support, or apparent conflict.
2. Project timing: newly created during the submission period, or meaningfully extended with Codex and/or GPT-5.6 after July 13 at 18:00 CEST. For pre-existing work, verify clear separation and timestamped evidence such as Codex logs or dated commits.
3. Project requirements: fits one category; genuinely uses Codex and GPT-5.6; installs/runs consistently on the stated platform; works as depicted; authorized use of third-party SDKs, APIs, data, hardware, and licenses.
4. Submission fields: English materials (or English translations), accurate category, clear feature/functionality description, public YouTube demo under 3 minutes with audio showing what was built and how Codex and GPT-5.6 were used.
5. Repository and testing: repository URL is public with relevant licensing or privately shared with `testing@devpost.com` and `build-week-event@openai.com`; working demo/test build and any needed credentials/instructions are available free and without restriction through the judging period.
6. README and Codex evidence: README explains collaboration with Codex, acceleration, human product/engineering/design decisions, and GPT-5.6/Codex contribution; submission includes the `/feedback` Codex Session ID for the main project thread.
7. Plugins/dev tools, when applicable: installation instructions, supported platforms, and a judge-ready demo, sandbox, test account, or equivalent that does not require rebuilding from scratch.
8. Rights and safety: original/owned submission; open-source and third-party license compliance; permissions for trademarks, music, images, data, privacy/publicity rights; no secrets, PII, malware, or unsupported claims in submitted materials.
9. Multiple entries, when applicable: each submission is unique and substantially different.
10. Conduct and support: no tampering, inappropriate conduct, prohibited financial/preferential sponsor support, or unmonitored paid OpenAI usage beyond available credits.
11. Post-submission: no substantive modifications after the deadline; keep testing access healthy through judging; monitor official messages and promptly handle any clarification, infringement/privacy correction request, hardware-access request, or winner verification forms.

## Report

Write or update exactly one idempotent report for the current run at `reports/build-week-rules-awareness/YYYY-MM-DD-HHMM.md`. Include:

- a one-line overall status;
- exact next deadline and time remaining in Europe/Berlin;
- findings grouped by the four labels, with evidence paths or page names;
- a short prioritized action list for Janik;
- sources checked, sources unavailable, and uncertainty;
- a reminder that the official rules and official notices control.

The task result shown to Janik must contain the overall status, the next deadline, and every confirmed issue or urgent possible risk. If nothing is wrong, explicitly say `CLEAR` and still name the next checkpoint.
