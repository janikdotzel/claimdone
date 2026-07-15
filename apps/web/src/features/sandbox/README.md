# Local portal run control

The V1 sandbox exposes five server-internal POST routes for packet-bound portal
runs:

- `/api/internal/portal-runs/setup` accepts the closed `PortalRunSetup` V4 body.
- `/api/internal/portal-runs/release` accepts `PortalRunRelease` after review and
  removes only the active-run authority.
- `/api/internal/portal-runs/abort` accepts `PortalRunRelease` while still in
  draft and removes the draft session plus its active-run authority.
- `/api/internal/portal-runs/inject-render-fault` arms one fixed,
  non-sensitive scalar mismatch in rendered review reads only.
- `/api/internal/portal-runs/repair-render-fault` removes that same bound
  mismatch, increments the portal version once, and leaves raw values intact.

All five require an exact `X-ClaimDone-Portal-Control` header matching the
server-only `CLAIMDONE_PORTAL_CONTROL_TOKEN`. The configured token must be 32 to
512 visible ASCII characters. Missing, malformed, or unequal credentials return
an empty 404 response and are never passed to the portal store. Do not use a
`NEXT_PUBLIC_` variable for this token.

Setup pre-stages only the three ordered attachment IDs. The eight scalar fields
remain empty until the public draft endpoint receives one version-current,
type- and value-exact copy of the privately bound expected fields. An active run
blocks review before that write and blocks public reset, delete, and reset-all
operations. Release never changes the reviewed values. Used run IDs are rejected
for the lifetime of this in-memory local sandbox store.

The fault commands bind the run, case, variant, current review version, and one
canonical scalar field. They never accept attachments or a replacement value.
Injection does not mutate the session, audit, or version. Repair adds one
value-free audit entry and one version increment; a replay, second field, or
release with a pending fault fails closed.

This is local Build Week sandbox authority, not a production control plane.
