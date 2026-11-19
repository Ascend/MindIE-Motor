# [2026-11-17] External copyright with a complete Mulan header was rejected

- **Symptom**: `check_header.py --check` reported a missing license for an external contributor's complete Mulan PSL v2 header.
- **Root cause**: `_has_license` recognized the Huawei copyright prefix rather than the complete license body.
- **Why**: Copyright ownership and the project's license declaration were treated as the same identifier.
- **Fix**: Preserve the existing Huawei recognition and accept a nonempty external copyright only when the entire remaining Mulan block matches.
- **Test interception**: Complete LF/CRLF headers pass unchanged; incomplete or altered bodies fail; automatic cleanup does not insert a second copyright into a valid external header.
- **Scenario**: New contributors retain their copyright while contributing under the project's existing license.
- **Keywords**: check-header, copyright, Mulan, external contributor.
