# INT-002 synthetic fixture

This directory contains the staged, non-identifying input definition for the V1
end-to-end run. The three PNG files are generated deterministically into
`.local/int002-fixtures/` by `scripts/generate_int002_fixtures.py`; generated
binary files are intentionally not committed.

The fixture is synthetic. Its names, policy reference, registration, address,
incident description, and images do not represent a person, insurer, vehicle,
or real event. `manifest.json` binds the exact image bytes, normalized statement
text, one clarification field, and one clarification answer used by INT-002.

Generate and verify the local files from the repository root:

```sh
./.venv/bin/python scripts/generate_int002_fixtures.py
./.venv/bin/python scripts/generate_int002_fixtures.py --check
```

`--json` emits a machine-readable descriptor with local paths and public
fixture digests. It never emits the statement text.
