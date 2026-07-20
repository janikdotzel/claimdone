# ClaimDone — OpenAI Build Week submission checklist

Last updated: July 20, 2026.

Submission deadline: **July 21, 2026 at 5:00 PM PT / July 22 at 02:00 CEST**.

## Next action

Complete the remaining OpenAI Build Week submission fields on Devpost, preview the entry, and explicitly submit it to the hackathon.

Required Devpost `/feedback` Session ID:

```text
019f69f5-252c-7fd1-a0fe-0e9ca08aea16
```

## Demo video

- [x] Record and edit the English demo video.
- [x] Keep the final video under three minutes. Verified duration: **2:49**.
- [x] Upload the video to YouTube.
- [x] Confirm that the video link is reachable.
- [x] Confirm that the video is public. YouTube metadata reports `isUnlisted: false`.
- [x] Remove the YouTube age restriction and confirm that the video plays inside the Devpost embed.
- [x] Add the YouTube URL to the Devpost project.

**Video:** [ClaimDone — AI Insurance Claims with Codex & Computer Use | OpenAI Build Week](https://www.youtube.com/watch?v=bSIkn1L5J44)

Final playback check:

- [ ] The working product is clearly visible.
- [ ] The narration is in English.
- [ ] The narration explains what ClaimDone does.
- [ ] The narration explains how Codex was used to build it.
- [ ] The narration explains how GPT-5.6 is used in the product.
- [ ] No API keys, personal data, or private notifications are visible.
- [ ] Audio is understandable at normal volume.

## Repository and documentation

- [x] Working application implemented.
- [x] Repository published at [github.com/janikdotzel/claimdone](https://github.com/janikdotzel/claimdone).
- [x] Repository is public.
- [x] Synthetic sample evidence is included.
- [x] Customer-facing UI is in English.
- [x] Add an open-source license: MIT.
- [x] Update `README.md` so `/demo` is the primary newcomer and judge route.
- [x] Mention `/` only as the optional simplified customer view.
- [x] Keep `CLAIMDONE_SHOW_COMPUTER_USE_BROWSER` documented as an optional presenter setting.
- [x] Expand the README explanation of how Codex accelerated the workflow and where key decisions were made.
- [x] Clearly document the runtime roles of GPT-5.6, `gpt-4o-mini-transcribe`, and `gpt-5.4-mini` Computer Use.
- [x] Remove the finished local recording script; it is no longer needed for judging or project setup.
- [x] Remove the generated local `next-env.d.ts` development-only diff before the final commit.
- [x] Confirm that `.env.local` and API keys are absent from tracked files and the final diff.
- [x] Commit and push all approved submission-documentation changes.

## Final technical verification

- [ ] Test setup from a clean checkout.
- [ ] Run `npm ci`.
- [ ] Create `.env.local` from `.env.example` and add a test API key locally.
- [x] Run `npm run lint`.
- [x] Run `npm run typecheck`.
- [x] Run `npm test` — 15 files and 121 tests passed.
- [x] Run `npm run build`.
- [ ] Run `npm run dev`.
- [ ] Open `http://127.0.0.1:3001/demo`.
- [ ] Complete the image-analysis and missing-information flow.
- [ ] Complete the Computer Use insurer-sandbox handoff.
- [ ] Confirm that the agent stops before any real submission.

## Devpost project

Current status at the last audit: **project published on Devpost, but not finally submitted to OpenAI Build Week**.

- [x] Replace the outdated project description with the completed product story.
- [x] Correct and finalize the tagline.
- [ ] Select submitter type. Expected: `Individual`.
- [ ] Select country of residence. Expected: `Germany`.
- [ ] Select category. Recommended: `Apps for Your Life`.
- [x] Add the repository URL: `https://github.com/janikdotzel/claimdone`.
- [x] Add the public YouTube URL: `https://www.youtube.com/watch?v=bSIkn1L5J44`.
- [x] Retrieve the required `/feedback` Session ID: `019f69f5-252c-7fd1-a0fe-0e9ca08aea16`.
- [ ] Add the required `/feedback` Session ID to Devpost.
- [x] Verify the Built With list.
- [ ] Verify the project thumbnail and screenshots.
- [ ] Add optional judge instructions explaining that `/demo` is the intended starting route.
- [ ] Review the final description in the submitter's own voice.

Suggested Built With entries:

- OpenAI
- Codex
- GPT-5.6
- OpenAI Responses API
- Computer Use
- Next.js
- React
- TypeScript
- Playwright
- Zod

## Final submission

- [ ] Preview the Devpost entry while signed out or in a private browser window.
- [ ] Check every public link.
- [x] Re-read the official rules and the latest announcement.
- [ ] Confirm that the entry still shows the correct category and repository.
- [ ] Submit before the deadline.
- [ ] Confirm that the project status changes from `submission_draft` to `Submitted`.
- [ ] Save the final Devpost URL and a screenshot of the submitted status.

## Official links

- [OpenAI Build Week](https://openai.devpost.com/)
- [Submission requirements and FAQ](https://openai.devpost.com/details/faqs)
- [Official rules](https://openai.devpost.com/rules)
- [ClaimDone on Devpost](https://devpost.com/software/claimdone)
- [ClaimDone demo video](https://www.youtube.com/watch?v=bSIkn1L5J44)
- [ClaimDone repository](https://github.com/janikdotzel/claimdone)
