# Build Week Diary

## July 12, 2026 - Pre-kickoff: choosing a Build Week bet

The first meaningful Build Week work was not implementation. It was idea selection.
The user opened the project with an explicit goal: find an OpenAI Build Week idea
that could plausibly win. The bar was not a useful document processor or a tidy
automation demo. The user wanted something that would make people feel that AI
could already do something surprising in the real world, ideally by combining
Codex, GPT-5.6, external tools, hardware, or visible actions.

The initial framing was practical and competitive. The user emphasized that a
winning project should make GPT necessary, not just decorative. A successful demo
needed a visible transformation from a natural-language prompt into a real-world
outcome, and it needed to avoid the common trap of showing only agent logs. The
brainstorm therefore used a stricter pattern: an unseen goal or fault supplied by
a judge, model reasoning over the situation, Codex changing software or control
logic, a visible result in the world, and independent verification that the result
actually worked.

The first winner candidate was a hardware-centered concept called Reality
Compiler or Codex Pit Crew. The idea was to place an ESP32 beside safe prewired
modules such as sensors, LEDs, a buzzer, and a servo. A judge would describe a new
behavior, Codex would write and flash firmware, then a judge would introduce a
hidden fault. The system would diagnose from video, serial logs, wiring notes,
documentation, and source code, patch the behavior, reflash, and prove the device
worked. This had a strong "spoken idea becomes a physical machine" moment, but it
depended on hardware access and setup time.

The same brainstorm produced several alternate directions. Ghost Hands would use
a camera and projector to turn a physical object into its own repair interface.
Universal Physical API would use a camera and constrained button-pushing hardware
to operate devices with no API. Meeting Room Medic would repair a broken hybrid
meeting room by diagnosing camera, microphone, display, lighting, and software
state. Assistive Object Forge would generate a personalized physical adapter for
someone with a specific grip or accessibility need. Civic Fix Loop appeared as a
lower-hardware option: one photo of a streetlight or overflowing bin becomes a
real civic report prepared through the correct local workflow.

The work became more concrete when the local machine was inspected. It already
had an OBSBOT Meet 2 camera, a RODE NT-USB Mini microphone, OBS, Zoom, Slack, and
Chrome. That changed the recommendation. Instead of buying robotics, the stronger
no-purchase path became a self-healing production studio. The concept was first
framed as One-Sentence Studio: the user says something like "Turn this desk into a
polished product launch," and Codex creates the visual package, configures the
camera, microphone, OBS, and meeting software, verifies the remote feed, and then
recovers after a judge breaks an audio or video setting.

That idea was renamed and sharpened into Stagehand. Its retellable demo was:
"They asked AI to turn a desk into a studio. It built the broadcast, we broke it,
and it repaired itself live." The important shift was that Stagehand should not
be a one-time desk configuration tool. The user correctly challenged that
weakness: if the product only sets up a permanent home office once, repeat value
is thin. The answer was to make the unit of work a session rather than a desk.
Stagehand would be useful when the location, available devices, privacy
requirements, occasion, audience, or destination changed. At an unchanged desk it
would act as a fast preflight and self-healing check; in a new context it would
compile the best available setup from whatever hardware was currently connected.

The user then narrowed the decision to two candidates and asked for a hackathon
specification: Stagehand and Civic Fix Loop. Stagehand remained the higher-ceiling
demo because it joins natural-language intent, real device inventory, OBS control,
overlay generation, fresh screenshot verification, deterministic audio checks,
and autonomous recovery from a microphone failure. Civic Fix Loop was kept as a
serious fallback because it has a clearer civic-impact story and a lower hardware
risk, but it needs to feel like an adaptive routing and evidence product rather
than a generic form filler.

The resulting specification, `BUILD_WEEK_SPEC.md`, fixed a shared architecture
for both candidates: a Codex-native task or skill in the ChatGPT desktop app as
the orchestrator, with narrow local helpers or MCP-style adapters for
deterministic operations. The local web UI would be only a capture and status
surface. It would not magically inherit Codex's Browser, Computer Use, permission,
or subagent capabilities. This boundary mattered because both ideas depend on
desktop or web workflows that ordinary public APIs do not expose.

For Stagehand, the spec made the first feasibility gate very specific. By July
14, it should enumerate the real devices, apply one observable OBS change, capture
the real output, and measure the selected microphone. The spec also recorded a
known risk: the inspected machine had OBS 27.2.4, where WebSocket control was not
bundled, and no WebSocket plugin had been observed. The next implementation
decision would therefore be either to install and pin a compatible obs-websocket
plugin or upgrade OBS to a compatible version with bundled WebSocket support.
Computer Use was allowed only as a narrow fallback path after the required macOS
permissions were verified.

The Stagehand MVP was intentionally constrained. It would support macOS, a home
profile using the OBSBOT camera and RODE microphone, a portable profile using
built-in devices or AirPods, two session intents, one dedicated OBS scene
collection, one stable HTML/CSS overlay template, a schema-validated session
plan, a fresh visual verification pass, deterministic microphone signal checks,
and recovery from a missing microphone. Arbitrary cameras, generalized
troubleshooting, Zoom integration, and broad snapshot/restore behavior were
deferred or explicitly excluded.

Civic Fix Loop was specified around Berlin as a working assumption, because the
project context was Berlin but the user's city had not been explicitly confirmed.
The MVP supports two different official workflows to prove that routing matters:
illegal dumped or bulky waste through Berlin Ordnungsamt-Online, and damaged
public streetlights through Stromnetz Berlin's map-based reporting workflow. The
streetlight route is technically different enough to justify the product: it has
map-selected assets, fault types, privacy acceptance, optional images, and no
dependable public case number, whereas the waste flow has a different authority,
different required fields, and public status semantics that must not be
overclaimed.

The Civic design also set a clear safety boundary. Because city services do not
offer stable APIs for these workflows, the product would need Browser or Computer
Use to operate rendered official forms. But the agent must stop before
consequential submission. Its tool surface should expose only a `fill_until_review`
operation, with no submit action. The user would review the destination, exact
fields, attachments, and data-sharing implications, then perform the final click
manually. This keeps the wow moment of "one photo becomes a real report packet"
without handing legal or civic responsibility to the agent.

By the end of the session, the project had a decision framework rather than a
single irreversible choice. The official Build Week window was documented as July
13 at 9:00 a.m. PDT through July 21 at 5:00 p.m. PDT, which is July 13 at 18:00
CEST through July 22 at 02:00 CEST in Berlin. The plan was to run bounded
feasibility spikes after the challenge opens, then choose one primary entry by
July 14. Stagehand should be chosen if OBS and device control can be made reliable
enough to create, verify, break, and repair the studio twice. Civic Fix Loop
should become primary if Stagehand's desktop integration risk is not contained.

## Sources and notes

- Reviewed the Codex task "Find build week wow ideas" in this project. The
  available history covers July 12, 2026, including the initial brainstorm, the
  hardware-aware narrowing to Stagehand, and the request to document Stagehand and
  Civic Fix Loop as a Build Week specification.
- Reviewed `BUILD_WEEK_SPEC.md`, which was produced from that task and contains
  the detailed candidate specifications, schedules, technical boundaries,
  acceptance criteria, and source links.
- No separate dated section was added for July 13, 2026 because the reviewed
  evidence did not show separate Build Week product work on that local date beyond
  this nightly documentation automation.
- Uncertainty: Berlin is documented as a working assumption for Civic Fix Loop,
  not as a user-confirmed city. The diary preserves that distinction.
