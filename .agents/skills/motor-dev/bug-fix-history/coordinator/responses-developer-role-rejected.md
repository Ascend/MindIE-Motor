# [2026-09-03] Responses input items were rejected by Chat message validation

- **Symptom**: `POST /v1/responses` returned HTTP 400 for a valid `developer` message or an explicitly typed non-message item such as `function_call_output` because it reported missing `role` or `content`.
- **Root cause**: `motor/coordinator/api_server/inference_server.py::_validate_responses_request` passed the Responses input-item union to the Chat-specific `_validate_message_array` function.
- **Why**: The implementation assumed every Responses array item was a Chat message, although native Responses input also carries typed tool, reasoning, and other items with different required fields.
- **Fix**: Keep Chat validation separate. Validate message-shaped Responses items with Responses roles, and defer other non-empty typed item schemas to the native engine while preserving the original request body.
- **Test interception**: Validation tests cover string, implicit/explicit message, `developer`, `function_call_output`, `reasoning`, and `file_search_call` inputs plus Responses-specific invalid-item errors. The HTTP test verifies that a typed non-message item reaches the mocked backend.
- **Scenario**: A client sends array-form Responses input containing developer instructions, tool output, or prior typed response items.
- **Keywords**: coordinator, responses, input-item, developer-role, function-call-output, validation, HTTP-400
