# Review: AI Agent Harness Design (idea.md)

เอกสารนี้ review ดีไซน์ใน `idea.md` เพื่อหา gap และรวบรวม open questions ก่อนแตกเป็น implementation tasks

---

## 1. จุดแข็งของดีไซน์ (สิ่งที่ควรคงไว้)

* **แยกหน้าที่ชัด**: LLM ตัดสินใจ / Harness validate + execute / Observation persisted — เป็น core loop ที่ถูกต้องและทดสอบได้
* **Database เป็น source of truth** ทำให้ worker stateless และ resume ได้จากทุก instance — สอดคล้องกับเป้าหมาย
* **Immutable skill revisions** + evaluation ก่อน publish — ป้องกัน self-learning ทำระบบพัง และ rollback ได้
* **Progressive tool disclosure** — แก้ปัญหา token/context noise ได้จริงเมื่อ MCP เยอะ
* **Risk-tiered permissions ตรวจที่ application layer** — ถูกต้อง ไม่พึ่ง system prompt
* **MVP cut สมเหตุสมผล** — ตัด multi-agent, autonomous self-learning, vector search ออกก่อน

---

## 2. Gap และความเสี่ยงที่พบ (ยังไม่มีคำตอบใน idea.md)

### G1. Concurrency และ crash semantics ยังไม่สมบูรณ์
ดีไซน์บอกว่าใช้ Redis lock สำหรับ active-run coordination แต่ยังไม่ตอบ:
* Worker crash กลางทาง → run ค้างสถานะ `Running` ตลอดไป ต้องมี **lease/heartbeat + reaper** ที่ requeue run ที่ lease หมดอายุ
* Lock expiry ระหว่างที่ LLM call ยังไม่จบ → สอง worker ทำ run เดียวกันพร้อมกัน ต้องมี **optimistic version บน `agent_runs`** (fencing token) กันเขียนทับ ไม่ใช่พึ่ง Redis lock อย่างเดียว

### G2. Tool execution idempotency ไม่ถูกพูดถึง
Crash **หลัง tool ทำงานสำเร็จแต่ก่อน persist observation** → resume แล้วยิงซ้ำ กรณี `send_email` หรือ `create_issue` คือ side effect ซ้ำ ต้องมี:
* `tool_executions` เขียนสถานะ `started` **ก่อน** execute (write-ahead) + idempotency key
* Policy ต่อ tool: retryable / non-retryable / require-check-before-retry

### G3. Streaming ขัดกับ stateless worker
Token streaming ต้องมีเส้นทาง Worker → Gateway → Client แต่ดีไซน์ไม่ได้บอกกลไก (Redis pub/sub? SSE ผูกกับ gateway instance ไหน?) และไม่ได้บอกว่า client reconnect กลาง run แล้ว catch up จากไหน (ควร replay จาก `agent_messages` + subscribe ต่อ)

### G4. MCP connection lifecycle vs stateless worker
MCP server แบบ stdio เป็น **stateful process** — worker stateless จะจัดการยังไง:
* Spawn ต่อ call (ช้า) / connection pool ต่อ worker / MCP proxy sidecar / รับเฉพาะ HTTP-based MCP
* ต้องมีตาราง `mcp_servers` registry (endpoint, auth, health) ซึ่งยังไม่อยู่ใน schema

### G5. Context growth ระหว่าง run ยาว
ReAct หลายสิบ step → context โต ดีไซน์บอก truncate/summarize ผล tool แล้ว แต่ยังไม่ตอบว่า **conversation + observations สะสม** จะถูก compact ยังไง (rolling summary? checkpoint summary เก็บที่ไหนใน schema?)

### G6. Completion Detector ไม่มี mechanism
แนะนำใช้ **explicit `finish` tool** (LLM ต้องเรียกพร้อม final answer + structured result) แทน heuristic — deterministic กว่าและ validate output schema ได้

### G7. Skill selection mechanism ไม่ระบุ
มี Skill Selector ใน diagram แต่ไม่บอกว่าเลือกยังไง: keyword match / LLM เลือกจาก summary list / embeddings และ inject กี่ skill ต่อ run, inject ตอนไหน (system prompt? เป็น tool ให้ LLM ขอเอง?)

### G8. Schema ยังขาดตาราง
`idea.md` มี 7 ตารางหลัก แต่ยังขาด: `tenants`/`users`, `tool_registry`, `mcp_servers`, `memories`, `credentials/secrets`, และ **ไม่มี `tenant_id`** ในตารางใดเลย ทั้งที่ guardrails บอกต้องแยก tenant

### G9. Credential management สำหรับ downstream tools
Agent เรียก Elastic/GitLab/Email ด้วย identity ของใคร — service account กลาง หรือ per-user token (on-behalf-of)? เก็บ secret ที่ไหน (DB encrypted, Vault, env)? เรื่องนี้กระทบ security model ทั้งระบบแต่ไม่อยู่ในดีไซน์

### G10. Approval flow ยังไม่จบ loop
มีสถานะ `WaitingApproval` แต่ไม่บอก: แจ้ง approver ทางไหน, approve ผ่าน UI ไหน, pending approval หมดอายุแล้วเกิดอะไร, approve แล้ว run ถูก re-enqueue ยังไง

### G11. Evaluation pipeline เป็น dependency ที่หนักที่สุดของ self-learning
"Run automated evaluation" พูดง่ายแต่คือระบบใหญ่ (test case corpus, LLM-as-judge, regression baseline) — ควร**เลื่อน self-learning ออกจาก MVP ทั้งก้อน** แต่เก็บ `skill_usage` + outcome logging ตั้งแต่วันแรก เพื่อให้มีข้อมูลตอนกลับมาทำ

### G12. LLM Gateway scope ไม่ชัด
Retry/fallback ข้าม provider, cost accounting, prompt caching, rate limit ต่อ tenant — build เอง หรือใช้ LiteLLM/OpenRouter? กระทบ interface ของ runtime โดยตรง

---

## 3. Open Questions

### P0 — ต้องตอบก่อนเริ่ม implement (blocking)

> **สถานะ: ตอบครบแล้ว** — ดูคำตอบและผลกระทบใน `decisions.md`

| # | คำถาม | ตัวเลือก/ข้อสังเกต |
|---|---|---|
| Q1 | ภาษาหลักของระบบ? | Python+FastAPI (iterate เร็ว, MCP SDK แข็งแรง) vs .NET 10 (ecosystem เดิม) — เอกสารเสนอทั้งคู่ ต้องเลือกหนึ่ง |
| Q2 | LLM provider แรก และทำ LLM Gateway ยังไง? | Claude API / OpenAI / ผ่าน LiteLLM — และใช้ native tool-calling (แนะนำ) หรือ text-based ReAct parsing |
| Q3 | Channel แรกของ MVP? | REST API + Simple Web UI ก่อน (แนะนำ) แล้วค่อย Slack/LINE — กำหนด contract กลางตั้งแต่แรก |
| Q4 | Infra ของ MVP ลดเหลือแค่ไหน? | Full stack (PG+Redis+RabbitMQ) vs **PG-only** (queue ด้วย `SELECT ... FOR UPDATE SKIP LOCKED`, lock ด้วย advisory lock) — PG-only ลด moving parts ครึ่งหนึ่งสำหรับคนเดียว |
| Q5 | Multi-tenant ตั้งแต่วันแรกไหม? | อย่างน้อยควรมี `tenant_id` ในทุกตารางแม้ยังใช้ tenant เดียว — retrofit ทีหลังเจ็บมาก |
| Q6 | Agent เรียก downstream tools ด้วย identity ไหน? | Service account กลาง (ง่าย, เริ่มก่อน) vs per-user on-behalf-of (ปลอดภัยกว่า, ซับซ้อน) |

### P1 — ตอบระหว่าง detailed design (ก่อน implement component นั้น)

> **สถานะ: ตอบครบแล้ว** — ดู `decisions.md` (D5–D8 และตาราง P1/P2)

| # | คำถาม |
|---|---|
| Q7 | Checkpoint granularity: persist ทุก LLM call หรือทุก tool call? หน่วยของ resume คืออะไร? |
| Q8 | Idempotency policy ต่อ tool ประเภท write — retry ยังไงหลัง crash (ดู G2)? |
| Q9 | Streaming mechanism: Redis pub/sub → SSE ที่ gateway? Reconnect/catch-up ยังไง (ดู G3)? |
| Q10 | MCP connection strategy: pool ต่อ worker / proxy sidecar / HTTP-only (ดู G4)? |
| Q11 | Completion: ใช้ explicit `finish` tool + output schema validation ไหม (แนะนำ: ใช่)? |
| Q12 | Context compaction: rolling summary เก็บใน `agent_steps` หรือแยกตาราง? trigger ที่กี่ token? |
| Q13 | Skill selection: MVP ใช้วิธีไหน — inject skill list ให้ LLM เลือก หรือ match จาก request? |
| Q14 | Tool result summarization: rule-based truncation อย่างเดียว หรือมี LLM summarize step (ค่า cost เพิ่ม)? |
| Q15 | Approval UX: approve ที่ Web UI พอไหมสำหรับ MVP? pending approval มี expiry ไหม? |
| Q16 | Secret storage: encrypted column ใน PG พอไหม หรือต้อง external (Vault/KMS)? |
| Q17 | Token/step budget กำหนดที่ระดับไหน — ต่อ run / ต่อ user / ต่อ tenant ต่อเดือน? |

### P2 — เลื่อนได้หลัง MVP

| # | คำถาม |
|---|---|
| Q18 | Evaluation pipeline สำหรับ skill: LLM-as-judge หรือ assertion-based? minimum sample size เท่าไหร่? |
| Q19 | Memory consolidation: dedupe/classify ด้วยอะไร? embedding store ใช้ pgvector ไหม? |
| Q20 | Scheduler HA: APScheduler in-process พอไหม หรือย้ายไป DB-driven / Temporal? |
| Q21 | Multi-channel (Slack/LINE): webhook contract และ session mapping ต่อ channel |
| Q22 | Sub-agents / multi-agent orchestration model |

---

## 4. Proposed Task Breakdown (draft — ปรับตามคำตอบ P0)

### Phase 0 — Foundation
1. ตัดสินใจ P0 (Q1–Q6) และบันทึกเป็น `decisions.md`
2. Repo scaffold + Docker Compose (PG อย่างน้อย) + migration tooling
3. Core schema v1: `tenants`, `users`, `agent_runs`, `agent_messages`, `agent_steps`, `tool_executions` (มี `tenant_id`, version column, idempotency key ตั้งแต่แรก)

### Phase 1 — Core Loop (หัวใจ ต้องเสถียรก่อนอย่างอื่น)
4. Run state machine + repository layer (create/load/checkpoint/resume, optimistic locking)
5. LLM Gateway v1 (provider เดียว, retry, token accounting)
6. Custom ReAct loop: LLM call → validate → execute → persist observation → loop
7. `finish` tool + completion handling
8. Guardrails v1: max steps, max tool calls, timeout, token budget, repeated-call detection
9. Native tools ชุดแรก (2–3 ตัว read-only) สำหรับทดสอบ loop จริง
10. Agent API v1: create run, send message, get run status/messages (sync ก่อน ยังไม่ streaming)

### Phase 2 — Tool System
11. Tool Registry (DB-backed) + tool abstraction เดียวสำหรับ native/HTTP/MCP
12. MCP Adapter + `mcp_servers` registry
13. Progressive tool disclosure (domain → summary → schema)
14. Result pipeline: normalize → truncate → persist → append
15. Permission tiers (ReadOnly/LowRiskWrite/SensitiveWrite/Destructive) + enforcement ที่ executor

### Phase 3 — Skills
16. Skill schema (`skills`, `skill_revisions`, `skill_usage`) + manual CRUD (ยังไม่มี self-learning)
17. Skill selection + injection เข้า context
18. Skill usage/outcome logging (เตรียมข้อมูลให้ self-learning ในอนาคต)

### Phase 4 — Human-in-the-loop + UX
19. Approval flow: `approval_requests` + WaitingApproval resume + Web UI approve
20. Streaming (pub/sub → SSE) + reconnect catch-up
21. Simple Web UI: run list, run detail (steps/tools/tokens), chat

### Phase 5 — Ops
22. OpenTelemetry tracing ครบ run lifecycle + metrics ตามรายการใน idea.md
23. Scheduler + queue worker สำหรับ scheduled runs
24. Reaper สำหรับ stale runs / expired approvals

> Self-learning (Phase 6+) เริ่มเมื่อ skill_usage มีข้อมูลพอและ evaluation pipeline พร้อม

---

## 5. คำแนะนำหลักจากการ review

1. **เลือก Q1 (ภาษา) ให้จบก่อน** — ทุกอย่างรอข้อนี้
2. **MVP ควรเป็น PG-only** ถ้าเป็นไปได้ — Redis/RabbitMQ เพิ่มทีหลังได้เมื่อมี load จริง แต่ schema ต้องออกแบบเผื่อ (queue table, advisory lock)
3. **ใส่ `tenant_id` + version column + idempotency key ตั้งแต่ migration แรก** แม้ยังไม่ใช้
4. **Phase 1 คือทั้งหมดที่สำคัญ** — ถ้า core loop + state machine + guardrails เสถียร ที่เหลือคือส่วนต่อขยาย
5. **เลื่อน self-learning ออกไป แต่เก็บ outcome data ตั้งแต่วันแรก** — มิฉะนั้นกลับมาทำตอนไม่มีข้อมูล
