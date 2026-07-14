# ClaimDone AI package

This package implements the isolated AI-001 through AI-003 core. It is not wired
to HTTP or persistence in this worktree.

- `ProviderConfig` accepts only the closed provider modes and exact V1 model IDs.
  Live mode is fixed to `gpt-5.6-sol` and `gpt-4o-transcribe`; SDK retries are
  fixed at zero and the app owns one extraction retry.
- `OpenAITranscriber` accepts only bounded, server-named PCM WAV bytes and makes
  one transcription call. It returns normalized text or a sanitized terminal
  `ProviderFailure`; it never retries.
- `ExtractionRunner` accepts exactly three content-addressed approved images and
  one approved statement or explicitly confirmed transcript. It uses multimodal
  Responses Structured Outputs and passes every response through canonical G2.
  Only G2 truncation, schema, or reference failures authorize the second call.
- `compose_neutral_narrative` uses only safe `observed` and `user_stated` facts.
  `build_visible_tool_plan` is deterministic and may include one clarification
  only when the canonical G5 result accepted it.

No adapter reads environment variables, logs request or response content, stores
remote request IDs, or persists images, audio, statements, prompts, or responses.
The production client factory requires an explicitly injected key and always sets
`max_retries=0`. Tests use injected fake clients and make no network requests.

`ProviderCallTelemetry` projects the same content-free operation, model, mode,
sequence, retry, and duration metadata into successful-call and terminal-failure
events. A retry event can be projected only from the completed initial extraction
call together with a canonical G2 run whose first deterministic result authorized
the single retry. The retry event describes that rejected response; the second
provider call is recorded separately with the next sequence and retry index one.
