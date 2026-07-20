# ClaimDone

ClaimDone turns one to three accident photos and a short written or spoken description into a reviewed insurance claim, then uses Computer Use to complete a synthetic insurer portal.

## Run locally

You need:

- Node.js 24
- an OpenAI API key
- Google Chrome for the Computer Use handoff

Clone the repository and install the dependencies:

```bash
git clone https://github.com/janikdotzel/claimdone.git
cd claimdone
npm install
cp .env.example .env.local
```

Open `.env.local` and add your server-side API key:

```bash
OPENAI_API_KEY=your_openai_api_key
```

To watch the isolated Chrome window while Computer Use runs, add this optional setting:

```bash
CLAIMDONE_SHOW_COMPUTER_USE_BROWSER=true
```

Start ClaimDone:

```bash
npm run dev
```

Open the presenter experience at [http://127.0.0.1:3001/demo](http://127.0.0.1:3001/demo). Three synthetic accident photos and a sample description are already prepared, so you can select **Analyze accident** immediately.

## Built with Codex and OpenAI

ClaimDone was planned, implemented, reviewed, debugged, and documented with Codex. The project began as a much broader insurance product, but that first direction was too complex for a focused hackathon demo. The most important improvement was changing how Codex was used before rebuilding it.

### Planning before implementation

We used Codex Plan mode with GPT-5.6 Sol at high reasoning effort to turn an open-ended idea into a deliberately small product. The planning process defined the four user-facing states, the one-question limit, the sandbox boundary, the presenter experience, and explicit non-goals such as dashboards, persistence, queues, authentication, and real insurer submission.

This let us review the product direction before generating the application. Questions and visual references were resolved while changes were still inexpensive.

### Building with milestone goals

Once the desired experience was clear, we created a precise Codex goal for implementation with GPT-5.6 Sol. The goal was divided into approval milestones covering the static experience, interactive flow, live AI integration, Computer Use handoff, and final polish.

Codex stopped after every milestone. We ran the app, reviewed it in the browser, gave concrete feedback, and approved the next stage only when the current one felt right. This kept a long-running agent aligned without losing the speed of autonomous implementation.

### Fast iteration and review

GPT-5.6 Terra at high reasoning effort was the main model for everyday product questions, smaller frontend and user-flow improvements, debugging, and final review. Codex also helped:

- simplify the original production-oriented repository into this standalone demo;
- implement the customer flow and presenter-only agent activity view;
- build and restrict the Computer Use browser loop;
- diagnose safety blocks and provider failures;
- review spacing, responsive behavior, accessibility, and demo pacing in the browser;
- write focused tests and prepare the repository, README, Devpost submission, and demo script.

The result came from a lightweight Codex setup with a small number of useful skills and tools, rather than a large collection of third-party extensions.

### Runtime model roles

| Model | Role in ClaimDone |
| --- | --- |
| `gpt-5.6` | Reviews the accident photos and customer statement, extracts claim details, and decides whether one essential detail is missing. |
| `gpt-4o-mini-transcribe` | Converts a short voice memo into text before the claim analysis. |
| `gpt-5.4-mini` | Uses the built-in Computer Use tool to navigate and complete the restricted local insurer sandbox. |

The application is built with Next.js 16, React 19, TypeScript, CSS Modules, the OpenAI Responses API, Playwright Core, and Zod. Claim data stays in memory, and the insurer portal is entirely synthetic.

## License

ClaimDone is available under the [MIT License](LICENSE).
