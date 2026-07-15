# INT-002 browser security boundary

The deterministic V1 composes the CU-001 browser harness with G6 tool authority, G7 portal-write
authority, persisted run state, and independent G8 verification for the exact local Portal A
fixture. It is defense in depth inside Chromium, Playwright, FastAPI, and the portal process; it is
not an operating-system or container network sandbox.

## Enforced and tested through INT-002

- HTTP requests and WebSockets are intercepted before navigation and allowed only for the exact
  configured `http://127.0.0.1:<port>` origin. Service workers are disabled.
- Chromium starts with QUIC, speculative preconnect, DNS prefetch, background networking, proxy
  use, extensions, and non-proxied WebRTC UDP restricted or disabled. Hostname resolution is
  mapped to failure except for the explicit IPv4 loopback host.
- A document-start guard blocks WebRTC/STUN peer connections, DataChannels, WebTransport,
  Dedicated/Shared Workers, Worklets, DNS-prefetch/preconnect DOM hints, host file-system picker
  APIs, and permission-bearing browser APIs. Attempts add a content-free, immutable policy block;
  page code cannot grant authority.
- Browser permissions start empty and are cleared before the first page. Downloads and popups are
  denied. File chooser events are latched and cleared without selecting a file.
- Real local Chromium tests exercise every capability above against TCP and UDP probes and verify
  that the probes receive no connection or packet.
- Provider `pending_safety_checks` are never acknowledged automatically. A valid nonempty check
  list terminates the run with a dedicated content-free block.
- Backend state, portal state, capability lifetime, and allowed fields are checked before and after
  every authority-bearing phase. G6 moves only `ready_to_fill` v5 into the bounded Portal A run.
  A read-only G7 preflight binds the exact claim values and three attachment identities before any
  portal write. After the bounded fill reaches Portal A review, G7 is finalized atomically before
  the backend reaches `verifying`. A portal UI signal cannot write backend `review`.
- G8 reads a fresh rendered snapshot. The first deterministic comparison must find exactly the
  rehearsed `incident_time` mismatch at Portal A v3. Review remains blocked until one authorized
  repair advances the portal to v4 and the second fresh comparison matches. Only that second pass
  may commit backend `review` v9.
- Agent capability cannot call human approval, submit, or obtain a receipt. The accepted browser
  flow stops with `agentCanSubmit=false`, no human-approved state, and no receipt.

The relevant upstream behavior is described by the [OpenAI computer-use
guide](https://developers.openai.com/api/docs/guides/tools-computer-use), the [Playwright browser
context API](https://playwright.dev/python/docs/api/class-browsercontext), and Chromium's
[network switch definitions](https://chromium.googlesource.com/chromium/src/+/HEAD/chrome/common/chrome_switches.cc).

## Residual risks and excluded later work

- JavaScript guards, Playwright routing, and Chromium flags do not protect against a compromised
  browser process, an unknown future browser network API, or traffic emitted below these hooks.
  Deployment must add an OS/container egress allowlist that permits only the intended loopback
  portal. Until that independent control exists, do not describe CU-001 as complete network
  isolation.
- Playwright applies explicit timeouts to Chromium launch, navigation, actions, screenshots, and
  provider calls, and the runner checks its 90-second deadline around every operation. The
  synchronous Playwright runtime bootstrap, context creation, and close APIs do not expose hard
  kill timeouts. A supervised worker/process boundary is required for a guaranteed kill deadline.
- The accepted V1 runtime uses the semantic `PlaywrightSemanticPortalBrowser` adapter. It does not
  claim an accepted live OpenAI Computer Use Responses loop.
- Portal B, a supervised hard-kill worker, broader prompt-injection testing, repeated reliability
  measurement, release, and production deployment remain excluded until a new user-approved goal.
