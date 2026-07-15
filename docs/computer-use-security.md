# CU-001 browser security boundary

CU-001 provides a bounded, fail-closed browser harness for the local Portal A sandbox. It is
defense in depth inside the Chromium and Playwright processes; it is not an operating-system or
container network sandbox.

## Enforced and tested in CU-001

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
- Backend `CaseState` is only a run guard: CU-001 may operate while the case remains
  `ready_to_fill` or `filling`. Portal completion comes from a separately injected, trusted
  case-bound review signal and produces the CU-owned status `portal_review_reached` only when the
  post-signal backend guard is exactly `filling`; it never claims or writes backend `review` or
  `blocked` state. The runner checks backend state both before and after reading that signal, so a
  stale portal signal or observed state drift cannot produce success.

The relevant upstream behavior is described by the [OpenAI computer-use
guide](https://developers.openai.com/api/docs/guides/tools-computer-use), the [Playwright browser
context API](https://playwright.dev/python/docs/api/class-browsercontext), and Chromium's
[network switch definitions](https://chromium.googlesource.com/chromium/src/+/HEAD/chrome/common/chrome_switches.cc).

## Residual risks and later ownership

- JavaScript guards, Playwright routing, and Chromium flags do not protect against a compromised
  browser process, an unknown future browser network API, or traffic emitted below these hooks.
  Deployment must add an OS/container egress allowlist that permits only the intended loopback
  portal. Until that independent control exists, do not describe CU-001 as complete network
  isolation.
- Playwright applies explicit timeouts to Chromium launch, navigation, actions, screenshots, and
  provider calls, and the runner checks its 90-second deadline around every operation. The
  synchronous Playwright runtime bootstrap, context creation, and close APIs do not expose hard
  kill timeouts. A supervised worker/process boundary is required for a guaranteed kill deadline.
- CU-001 returns a sanitized `BLOCKED` result and always attempts resource cleanup. Persisting the
  terminal workflow event atomically belongs to CU-002; CU-001 intentionally contains no workflow
  writer.
- CU-002 will later own the atomic backend transition from `filling` to `verifying`. Independent G8
  verification, not CU-001 or a portal UI signal, will own the later `verifying` to `review`
  transition.
