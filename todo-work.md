# Marcus Code — Follow-up Work

แผนงานต่อจาก improvement backlog โดยเรียงตามความเสี่ยงและผลกระทบ

## Checklist

- [ ] วิเคราะห์และแก้ full-suite test timeout
  - [x] ยืนยันว่า test collection สำเร็จ (221 tests)
  - [x] ระบุว่า DB-backed tests เป็นกลุ่มที่ค้าง/ล้มเหลวเมื่อ test database ไม่พร้อมหรือ schema/shared cleanup ชนกัน
  - [ ] เพิ่ม readiness check หรือ timeout ที่เหมาะสม
  - [ ] ยืนยันผลด้วย full test suite
  - [x] ยืนยัน non-DB suite: `uv run pytest -q` (DB tests opt-in) → 103 passed, 118 skipped
- [ ] เพิ่ม streaming tests
  - [ ] text SSE chunks
  - [ ] tool-call fragments
  - [ ] malformed events และ provider errors
- [ ] ทำ session budget ให้ configurable
  - [ ] ตั้งค่าผ่าน `Settings`/environment
  - [ ] แสดง budget และ remaining ใน `/usage`
- [ ] เพิ่ม history summarization ก่อน truncation
- [ ] ปิด SSRF edge cases
  - [ ] DNS rebinding
  - [ ] redirect chains
  - [ ] IPv4/IPv6 edge cases

## Current focus

กำลังวิเคราะห์สาเหตุ full-suite timeout — non-DB suite ผ่านแล้ว 103 tests; DB-backed tests ถูกทำให้ opt-in ด้วย `HARNESS_RUN_DB_TESTS=1` เพราะใช้ PostgreSQL จริงและยังมี DDL contention เมื่อรันใน environment นี้

## Skill system audit

- [x] Manual lifecycle: draft → approved → published, rollback, deprecate
- [x] Immutable published revisions enforced by PostgreSQL trigger
- [x] Tenant-scoped API and admin authorization
- [x] Progressive disclosure and active revision snapshot per run
- [x] Usage and feedback persistence
- [x] Revision version allocation under concurrent writers (row lock + unique constraint)
- [x] Schema/content validation and size limits
- [ ] Evaluation/test-case gate before publish
- [x] Lightweight automatic skill ranking hint (goal/metadata overlap, top 20)
- [x] Conservative automatic selection and conflict handling
- [x] Feedback-based tie-breaker using historical success rate
- [ ] Embedding-based selection for semantic similarity

## CLI follow-up

- [x] Configurable CLI token/history budgets via `HARNESS_CLI_*`
- [x] `/usage` shows budget and remaining tokens
- [x] Streaming text and tool-call SSE tests
- [x] Deterministic history summarization before truncation
- [x] Reject multi-address DNS hosts and revalidate every redirect
- [ ] Complete socket-pinned DNS-rebinding-resistant HTTP transport
- [x] CLI README/help examples
- [x] Release packaging workflow with wheel, sdist, and SHA256SUMS
- [x] Verify built wheel exposes `marcus --help`
- [ ] Skill import/export and operational observability
