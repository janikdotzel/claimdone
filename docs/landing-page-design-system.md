# ClaimDone Product Design System

## 1. Design thesis

**Product:** ClaimDone turns a few pieces of accident evidence into a structured, reviewable insurance claim.

**Audience:** Private drivers who are stressed after a minor accident and want to avoid long insurance forms.

**Single job of the product:** Make the promise immediately believable and carry it through every screen: three photos plus one short voice memo or text are enough for the agent to prepare a complete claim for human review.

**Signature element:** The landing page is built around one continuous evidence-to-claim flow. Three documentary photo cards and a short statement visibly converge into the Claim Agent and then into a clean claim document. This replaces the generic SaaS dashboard screenshot.

**Aesthetic risk:** Use real-looking accident evidence as the hero's dominant visual material. Keep everything around it quiet, precise, and calm so the page feels like a trustworthy claims workspace rather than an insurance advertisement.

## 2. Design principles

1. **Calm evidence, not drama.** Accident imagery is factual, daylight-balanced, and free of people, injuries, emergency scenes, or sensational damage.
2. **Show the transformation.** The page should always make the relationship `3 photos + memo/text → agent work → complete claim` visible.
3. **Explain automation.** Never represent the agent as a magic black box. Name its steps: read photos, order facts, check details, complete the claim.
4. **Document-like output.** The result should feel like a real, concise claim document with clear fields, provenance, completeness, and a ready state.
5. **One strong action.** Use one primary CTA per section. Avoid dashboards, decorative KPI cards, fake metrics, or invented customer logos.
6. **Plain English.** Use active, reassuring language. Prefer “Create insurance claim” over technical or abstract AI vocabulary.

## 3. Light visual identity

### Color tokens

| Token | Hex | Purpose |
|---|---:|---|
| Canvas | `#F4F7F5` | Page background and large quiet areas |
| Surface | `#FFFFFF` | Cards, forms, claim document |
| Ink | `#17211D` | Primary text and icons |
| Secondary ink | `#66736D` | Supporting copy and metadata |
| Line | `#DCE4DF` | Dividers, input borders, inactive structure |
| Claim green | `#25634F` | Primary actions, progress, active states, verified status |

Derive the soft brand surface from Claim green at 8–12% opacity. Do not add decorative gradients. If a gradient is used at all, limit it to a nearly imperceptible surface treatment on the completed claim.

### Typography

- **Display:** `Familjen Grotesk`, 500. Use for the hero and section headlines only.
- **Body and UI:** `Source Sans 3`, 400 and 500. Use for paragraphs, controls, labels, and navigation.
- **Evidence/data labels:** `IBM Plex Mono`, 400 and 500. Use sparingly for claim IDs, timestamps, small uppercase eyebrows, and provenance.

Recommended desktop type scale:

- Hero: `clamp(3rem, 6vw, 5.75rem)`, tight but readable line height
- Section title: `clamp(2rem, 4vw, 3.5rem)`
- Card title: `1.25rem`
- Lead: `1.25rem`
- Body/UI: `1rem`
- Metadata: `0.8125rem`, never smaller

### Layout and spacing

- Maximum content width: `1200px`
- Desktop grid: 12 columns with `24px` gutters
- Section spacing: `96px`; reduce to `64px` on tablet and `48px` on mobile
- Core spacing scale: `4, 8, 12, 16, 24, 32, 48, 72, 96`
- Card radius: `18px`; controls: `12px`; circular counters only where the photo sequence needs them
- Borders: `1px solid #DCE4DF`
- Shadows: use only on the main completed-claim surface; keep photo cards and inputs primarily border-defined
- Avoid dark hero sections, glassmorphism, glowing blobs, excessive pills, and floating decorative icons

## 4. Core hero module

### Hero copy

**Eyebrow:** `Report an accident without the paperwork`

**Headline:** `Three photos. One short statement. Your claim is ready to review.`

**Supporting copy:** `ClaimDone finds the important details, organizes what happened, and prepares a complete insurance claim for your review.`

**Primary CTA:** `Start my claim`

**Secondary CTA:** `See how it works`

### Evidence input

On desktop, make the hero a deliberate split composition: keep the compact headline, supporting copy, and CTAs in the left third; place the complete evidence-to-claim demo in the wider right column. Do not place an oversized headline above the demo.

Use three equal photo cards in this exact order:

1. **Overview** — both vehicles and the overall accident position
2. **Damage** — a close, legible view of the damaged area
3. **Context** — road layout, traffic signal, vehicle positions, and surrounding context

Each card contains one edge-to-edge 3:2 image, a 24px sequence marker, the short label, and a quiet check icon. Keep metadata outside the image. On mobile, cards stack vertically.

Place a compact **Memo / Text** segmented choice directly below the three photo cards in the right column. The default is Memo and shows a short waveform plus an editable statement. Use the label `What happened?` and display `Voice memo · 18 sec` or `Text · short statement` as metadata.

### Claim action

Place the primary action `Create insurance claim` immediately below the photo and Memo/Text inputs in the right column. On activation, update the claim document once and end in a stable complete state. Respect `prefers-reduced-motion`. Explain the four agent steps in the single section below the hero rather than inserting another large rail between input and result.

### Completed claim

The claim output is the strongest surface on the page. It should include:

- `Insurance Claim`
- claim ID
- `Complete` status
- completeness bar
- Time
- Location
- Damage
- Registration
- Injuries
- What happened
- provenance line: `Created from 3 photos + voice memo`
- final state: `Ready to review`

Keep the output concise. The result must look finished without resembling a dense enterprise dashboard.

## 5. Landing page architecture

1. **Navigation** — ClaimDone wordmark, `How it works`, and one CTA.
2. **Split hero thesis** — compact copy on the left; three photos, Memo/Text, `Create insurance claim`, and the completed claim document on the right.
3. **Transparent agent work** — the only section below the hero; show the four agent steps with one sentence of explanation each.
4. **Minimal footer** — wordmark, one short boundary statement, and contact.

Do not add benefit grids, a separate safety section, FAQ, testimonials, or a repeated final CTA. The working demo is the explanation.

## 6. Component rules

- **Buttons:** sentence case, verb-led labels, 44px minimum height, visible focus ring. One filled button per control group.
- **Cards:** use cards only for the three evidence inputs, the memo/text input, and the completed claim. Do not wrap every section in a card.
- **Forms:** labels always visible; placeholder text never replaces a label. Use explicit error guidance.
- **Icons:** Lucide-style 16–20px line icons, always paired with a visible label when actionable.
- **Status:** pair color with text and icon. Never communicate completeness with green alone.
- **Photography:** consistent incident, overcast daylight, realistic smartphone perspective, no readable personal data.
- **Motion:** one orchestrated transformation from evidence to claim; no looping ambient animation.

## 7. Responsive behavior

- **Desktop:** use a roughly one-third/two-thirds hero split. Keep copy only on the left; place the full evidence interaction and claim document on the right.
- **Tablet:** stack copy above the demo once the two-column composition becomes cramped; keep the three photos together when possible.
- **Mobile:** stack the three photo cards, then Memo/Text, then the claim action and claim fields. The agent explanation below the hero becomes a 2×2 grid.
- Keep the claim CTA and critical status visible without horizontal scrolling.
- Preserve photo crops with `object-fit: cover` and meaningful `object-position` values.

## 8. Accessibility and trust

- WCAG AA contrast for all body text and controls
- keyboard-accessible Memo/Text control and CTA
- meaningful alt text describing the evidence purpose, not decorative details
- visible focus states
- reduced-motion alternative
- no actual personal data in examples
- label example images as AI-generated in the footer or nearby metadata
- do not claim that the agent submits automatically unless the real product does so

## 9. Asset paths

Use the generated example images with `next/image`:

- `/images/claim-flow/accident-overview.jpg`
- `/images/claim-flow/accident-damage.jpg`
- `/images/claim-flow/accident-context.jpg`

## 10. Copy-paste prompt for a coding agent

```text
Build a polished, production-ready light landing page for ClaimDone in the existing project.

Product and audience:
ClaimDone helps private drivers after a minor accident. The landing page must make one promise immediately believable: three photos plus one short voice memo or text are enough for the Claim Agent to prepare a complete insurance claim for review.

Visual direction:
Use a calm, documentary, evidence-first design. The memorable signature is one continuous flow: three real-looking accident photo cards + Memo/Text → visible Claim Agent steps → a concise completed Insurance Claim. Do not use a generic SaaS dashboard screenshot, dark hero, glassmorphism, decorative gradients, glowing blobs, fake metrics, invented testimonials, or excessive feature cards.

Light design tokens:
- Canvas #F4F7F5
- Surface #FFFFFF
- Ink #17211D
- Secondary ink #66736D
- Line #DCE4DF
- Claim green #25634F
Derive soft green surfaces at 8–12% opacity. Use 18px card radii, 12px control radii, subtle 1px borders, and a 1200px max-width 12-column layout.

Typography:
Use Familjen Grotesk 500 for display headlines, Source Sans 3 400/500 for body and UI, and IBM Plex Mono 400/500 only for claim IDs, timestamps, small eyebrows, and provenance. Load fonts through the framework’s supported font system. Use a restrained responsive hero headline in the left column, capped around 4.25rem, with precise spacing and no oversized empty space.

Hero content:
Eyebrow: “Report an accident without the paperwork”
Headline: “Three photos. One short statement. Your claim is ready to review.”
Supporting copy: “ClaimDone finds the important details, organizes what happened, and prepares a complete insurance claim for your review.”
Primary CTA: “Start my claim”
Secondary CTA: “See how it works”

Hero layout and interaction:
Use a compact desktop split: copy and CTAs occupy the left third; the full evidence-to-claim demo occupies the wider right side.
Create three equal 3:2 photo cards in this exact order:
1. Overview — /images/claim-flow/accident-overview.jpg
2. Damage — /images/claim-flow/accident-damage.jpg
3. Context — /images/claim-flow/accident-context.jpg
Each card has an edge-to-edge image, a small sequence marker, the short label, and a quiet check icon.

Directly below the photos, add a keyboard-accessible Memo/Text segmented control. Default to Memo. Show a short waveform, the label “What happened?”, this example statement: “I was stopped at the light. The other vehicle struck the front-left side of my car while turning. No one was injured.”, and metadata “Voice memo · 18 sec”.

Below the evidence, add one primary button labeled “Create insurance claim”. On click, animate the result once and populate the claim fields. Respect prefers-reduced-motion. Do not insert another large feature rail between the inputs and the claim.

The completed claim is the strongest surface. Include:
- eyebrow “Insurance Claim”
- claim ID “Claim #CD-2048”
- status “Complete”
- 100% completeness bar
- Time: July 16, 2026 · 08:42
- Location: Intersection · identified from photo
- Damage: Front left · bumper
- Registration: B · CD 2048
- Injuries: None
- What happened: The other vehicle struck the front-left side while turning.
- provenance: “Created from 3 photos + voice memo”
- final state: “Ready to review”

Page sections after the hero:
1. One transparent-agent section with Read photos, Organize facts, Check details, and Complete claim.
2. A minimal footer.
Do not add benefit, safety, FAQ, testimonial, or repeated CTA sections.

Implementation requirements:
- Inspect and reuse the project’s existing Next.js, TypeScript, CSS, UI components, and conventions before adding anything.
- Use semantic HTML, next/image, meaningful alt text, visible labels, keyboard support, clear focus states, and WCAG AA contrast.
- Keep one primary CTA per section.
- Make the layout responsive down to 320px: photo cards stack, agent steps become 2×2, claim fields become one column.
- Do not add dependencies unless the existing stack cannot support the requirement.
- Do not invent submission behavior, customer logos, security certifications, metrics, or testimonials.
- Keep all copy in natural, plain English and use active verbs.
- Add focused tests for the Memo/Text toggle, generate interaction, and key rendered content.
```

## 11. Prompts used for the three AI example photos

### Overview

```text
Use case: photorealistic-natural
Asset type: landing-page example photo, card “Overview”
Primary request: realistic smartphone photo documenting a minor two-car accident at a modern European city intersection
Scene/backdrop: same incident throughout the set; overcast bright daylight; dark navy compact hatchback and silver sedan stopped after a low-speed collision
Subject: both vehicles visible in a clear overview, front-left corner of the navy car touching the silver sedan near the crossing
Style/medium: photorealistic natural documentary smartphone photography, believable everyday imperfections
Composition/framing: landscape 3:2, wide eye-level view, enough context to understand the accident
Lighting/mood: calm neutral daylight, trustworthy and factual, not dramatic
Constraints: no people visible, no injuries, no emergency vehicles, no readable license plates, no logos, no text, no watermark
Avoid: cinematic action, fire, smoke, dramatic damage, staged stock-photo look
```

### Damage

```text
Use case: photorealistic-natural
Asset type: landing-page example photo, card “Damage”
Primary request: realistic smartphone detail photo of the front-left damage from the same minor accident
Scene/backdrop: same overcast European city intersection and same dark navy compact hatchback as the overview image
Subject: close-up of a dented and scraped front-left bumper, slightly cracked headlight, realistic low-speed collision damage
Style/medium: photorealistic natural documentary smartphone photography, sharp material texture and believable paint scratches
Composition/framing: landscape 3:2, close three-quarter angle focused on the damaged front-left corner
Lighting/mood: calm neutral daylight, factual insurance documentation
Constraints: no people, no blood, no graphic content, no readable license plates, no logos, no text, no watermark
Avoid: catastrophic damage, showroom polish, cinematic lighting, staged stock-photo look
```

### Context

```text
Use case: photorealistic-natural
Asset type: landing-page example photo, card “Context”
Primary request: realistic smartphone context photo from the same minor two-car accident at a European city intersection
Scene/backdrop: same overcast daylight, same dark navy compact hatchback and silver sedan; traffic light, lane markings, curb and intersection geometry clearly visible
Subject: roadway context and final vehicle positions after the low-speed collision; license plates present only as naturally blurred unreadable shapes
Style/medium: photorealistic natural documentary smartphone photography, believable everyday street texture
Composition/framing: landscape 3:2, slightly wider contextual angle showing traffic signal and lane layout
Lighting/mood: calm neutral daylight, trustworthy and factual
Constraints: no people visible, no injuries, no emergency vehicles, no readable license plates, no logos, no text, no watermark
Avoid: dramatic scene, fire, smoke, severe wreckage, cinematic grading, staged stock-photo look
```

## 12. Product-wide application

The landing page and the claim experience are one continuous product. Do not introduce a second visual language, a dashboard shell, or an operations-style workspace after the primary CTA.

### Global shell

- Use the same light canvas, typography, spacing, border language, and Claim green across every public route.
- Keep the header quiet: wordmark, two or three useful anchors, and one primary action. Do not add a permanent sidebar.
- Use a centered reading width for guided tasks. Supporting checks may follow the main content on large screens, but they must stack after it on smaller screens.
- Keep internal component and workflow showcases outside the primary navigation and mark them `noindex`.

### Guided intake

Use one vertical path with three plain-language stages:

1. `Add evidence`
2. `Claim Agent`
3. `Review`

Start directly with the evidence controls. Keep the three photo purposes visible as `Overview`, `Damage`, and `Context`; do not collapse them into a generic multi-file uploader. Place `Text` and `Voice memo` in one segmented control labeled `What happened?`. Do not add a separate disclosure step or a permission-confirmation card. For the staged sandbox, keep the required deterministic consent flags intact and explain their effect in one short click-wrap sentence beside the create action.

Do not display validation errors before the user has started the relevant action. Error messages must explain what is missing and how to resolve it. Keep deterministic evidence, privacy, safety, and completeness gates authoritative; the agent may explain a failed check but may never override it.

### Claim Agent state

Present the agent as a short, observable transformation rather than a conversation or autonomous black box. Use the same four verbs everywhere: `Read photos`, `Organize facts`, `Check details`, `Complete claim`.

Show a concise progress state first. Put gate trails, evidence metadata, provenance details, and agent activity in a collapsed disclosure labeled `See how ClaimDone checked this claim`. The disclosure must remain keyboard accessible and must not conceal a blocking result.

### Secure claim review

The final review is a document, not a dashboard. Lead with `Your insurance claim`, a completeness indicator, and a small set of clearly labeled fields. Keep each extracted value connected to its source. End with the exact human boundary `Ready for human review`.

ClaimDone prepares and checks the claim; it does not silently approve, sign, or submit it. Any final submission or insurer action must be a separate, explicit user-controlled step and must only appear when the real product supports it.

### System states

- **Loading:** use a calm document skeleton and describe the current task in English.
- **Empty:** explain the next evidence action; never fill the screen with decorative cards.
- **Error:** state what failed, what remains safe, and offer one recovery action.
- **Not found:** use plain English, one sentence, and one route back to the product.
- **Demo/sandbox:** label synthetic data clearly, collapse developer controls by default, and never suggest that a fixture-only demo is a live insurer submission.

### English-only product copy

All customer-facing navigation, headings, controls, validation messages, disclosures, status labels, metadata, and accessibility labels must be natural English. German may be used in developer fixtures only when it is deliberately testing Unicode or input handling. Prefer short verbs and concrete nouns; avoid internal gate codes, workflow jargon, and claims-industry abbreviations in the main path.

## 13. Copy-paste prompt for the complete product

```text
Apply the ClaimDone Product Design System to every customer-facing page and make it the canonical product UI.

Product experience:
ClaimDone turns three accident photos plus one short text or voice statement into a structured insurance claim for human review. The entire journey must feel like one simple page sequence:
1. Add evidence: Overview, Damage, Context, plus Text or Voice memo
2. Claim Agent: Read photos, Organize facts, Check details, Complete claim
3. Review: a complete, source-linked insurance claim that is Ready for human review

Do not turn the product into a dashboard. Do not add a permanent sidebar, KPI tiles, activity feeds, dense tables, floating widgets, dark admin chrome, or generic SaaS cards. Use a calm, document-like, one-column journey with progressive disclosure for technical details.

Canonical visual system:
- Canvas #F4F7F5
- Surface #FFFFFF
- Ink #17211D
- Secondary ink #66736D
- Line #DCE4DF
- Claim green #25634F
- Display type: Familjen Grotesk 500
- Body/UI type: Source Sans 3 400/500
- Data/provenance type: IBM Plex Mono 400/500, used sparingly
- Maximum content width 1200px
- Spacing scale 4, 8, 12, 16, 24, 32, 48, 72, 96
- Card radius 18px; control radius 12px; 1px borders
- Use shadows only for the primary completed-claim document
- No decorative gradients, glassmorphism, glowing blobs, excessive pills, or ambient animation

Global behavior:
- Keep the light header minimal: ClaimDone wordmark, useful landing-page anchors, and one primary Start a claim action.
- Keep every route visually continuous with the landing page.
- Use one primary action per decision point.
- Put labels above inputs; placeholders never replace labels.
- Do not show validation errors before a user starts the relevant action.
- Use semantic HTML, visible focus states, keyboard support, WCAG AA contrast, and reduced-motion behavior.
- Make all layouts work without horizontal scrolling from 320px upward.
- Keep all customer-facing copy, validation, status, metadata, alt text, and accessibility labels in plain English.

Evidence intake:
- Preserve three explicit photo slots in this order: Overview, Damage, Context.
- Explain what each photo should contain.
- Keep a keyboard-accessible Text / Voice memo segmented control labeled What happened?
- State accepted formats and privacy expectations without exposing internal implementation jargon.
- Start directly with evidence; do not add a separate consent screen or confirmation card.
- Keep required sandbox consent flags inside the deterministic gate and use one concise click-wrap sentence beside the create action.

Agent and deterministic checks:
- Represent the Claim Agent with four observable steps: Read photos, Organize facts, Check details, Complete claim.
- Deterministic evidence, privacy, safety, provenance, and completeness gates always outrank model output.
- The agent may add a block or request clarification; it may never override a failed deterministic check.
- Show blocking results immediately in plain language.
- Move gate trails, evidence metadata, provenance, and agent activity into a collapsed, keyboard-accessible disclosure labeled See how ClaimDone checked this claim.

Final review:
- Render the result as a clean insurance-claim document, not a dashboard.
- Lead with Your insurance claim, completeness, and clearly labeled claim fields.
- Keep every extracted value connected to its source.
- End with Ready for human review.
- Never imply automatic approval, signature, insurer submission, or policy coverage unless those capabilities exist and the user explicitly performs the action.
- Clearly label synthetic sandbox data and keep developer/demo controls collapsed by default.

Routes and system states:
- Apply the system to the landing page, claim intake, Claim Agent state, secure final review, sandbox/demo pages, internal showcases, loading, error, empty, and not-found states.
- Keep internal showcases out of the primary navigation and mark them noindex.
- Loading, error, and not-found states must use the same light document language and offer one clear next action.

Implementation workflow:
- Inspect the existing Next.js, TypeScript, CSS, tests, API contracts, deterministic gates, and route structure before editing.
- Reuse existing components and tokens where they express this system; remove obsolete dashboard styling rather than layering over it.
- Preserve security, provenance, redaction, consent, and human-review behavior.
- Do not add dependencies unless the existing stack cannot meet the requirement.
- Do not invent customer data, live insurer connectivity, certifications, testimonials, metrics, or production capabilities.
- Add or update focused tests for English copy, the three evidence purposes, Text / Voice memo controls, deterministic boundaries, the completed claim document, human review, responsive navigation, and loading/error/not-found states.
- Run the repository's canonical lint, typecheck, test, and production-build commands, then report any deliberately retained demo limitations.
```
