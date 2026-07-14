# ClaimDone AI package

This package implements the isolated AI-001 through AI-003 core. It is not wired
to HTTP or persistence in this worktree.

- `ProviderConfig` accepts only the closed provider modes and exact V1 model IDs.
  Live mode is fixed to `gpt-5.6-sol` and `gpt-4o-transcribe`; SDK retries are
  fixed at zero and the app owns one extraction retry.
- `create_openai_client` requires explicitly injected API-key, organization, and
  project values and pins the API origin to `https://api.openai.com/v1`. OpenAI
  SDK environment defaults cannot redirect the client or select another tenant.
  Non-empty `OPENAI_CUSTOM_HEADERS` fails before construction, protected auth and
  tenant headers are supplied explicitly, and unrelated admin/webhook environment
  secrets are disabled with explicit empty values.
- `OpenAITranscriber` accepts only bounded, server-named PCM WAV bytes and makes
  one transcription call. It returns normalized text or a sanitized terminal
  `ProviderFailure`; it never retries.
- `ExtractionRunner` accepts exactly three content-addressed approved images and
  one approved statement or explicitly confirmed transcript. It uses multimodal
  Responses Structured Outputs and passes every response through canonical G2.
  Only G2 truncation, schema, or reference failures authorize the second call.
- Extraction results require a one-to-one binding between each completed provider
  response, its sequence/retry metadata, and the matching immutable G2 attempt.
- `compose_neutral_narrative` accepts a bound `NarrativeInput`, never bare facts.
  It maps only closed field values into fixed text. Every `observed` source must
  resolve through canonical provenance to approved image evidence; every
  `user_stated` source must resolve to approved user-statement evidence or a
  human-confirmed transcript. Free-form locations, damage descriptions, model
  narratives, instructions, and liability language are omitted.
  `build_visible_tool_plan` is deterministic and may include one clarification
  only when the canonical G5 result accepted it.

No adapter consumes environment-provided credentials, routing, tenant selection,
or headers. The client factory only checks whether `OPENAI_CUSTOM_HEADERS` is
non-empty so it can fail closed without retaining or logging the value. No adapter
logs request or response content, stores remote request IDs, or persists images,
audio, statements, prompts, or responses. The factory requires an explicitly
injected key and always sets `max_retries=0`. Tests use staged credentials or
injected fake clients and make no network requests.

`ProviderCallTelemetry` projects the same content-free operation, model, mode,
sequence, retry, and duration metadata into successful-call and terminal-failure
events. A retry event can be projected only from the completed initial extraction
call together with a canonical G2 run whose first deterministic result authorized
the single retry. The retry event describes that rejected response; the second
provider call is recorded separately with the next sequence and retry index one.
