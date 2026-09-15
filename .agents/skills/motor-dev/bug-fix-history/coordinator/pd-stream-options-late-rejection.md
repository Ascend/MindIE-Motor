# [2026-09-09] PD 分离请求在 Decode 才因 Prefill 改写字段被拒绝

- **Symptom**: An invalid OpenAI request completed Prefill, then Decode returned HTTP 400; the Prefill-side Mooncake request retained KV cache until its 480-second expiry.
- **Root cause**: The PD protocol adapter rewrites `stream`, `stream_options`, `max_tokens`, `max_completion_tokens`, and `min_tokens` for Prefill while Decode receives the client values, so engine-side validation could observe different inputs on the two legs.
- **Why**: The implementation relied on engine-side schema validation without accounting for fields intentionally rewritten between the Prefill and Decode legs.
- **Fix**: Validate the remaining rewritten fields and their cross-field constraints at Coordinator ingress. Existing max-token sanitization is retained, and raw `stream` truthiness remains aligned with the engine's before-validator behavior.
- **Test interception**: OpenAI validation tests cover Chat and Completion bodies, stream options, invalid `stream`/`min_tokens` types and ranges, maximum-token precedence, and compatible coercions.
- **Scenario**: PD separation receives an OpenAI request whose client generation parameters become valid only after the Prefill adapter rewrites them.
- **Keywords**: coordinator, PD separation, stream_options, min_tokens, Decode HTTP-400, KV cache expiry
