# Decisions

บันทึกการตัดสินใจจาก open questions ใน `review-open-questions.md` (P0 ครบแล้ว — เริ่ม implement ได้)

## D1. ภาษาหลัก: Python + FastAPI (Q1)

* Python 3.12+, FastAPI, Pydantic v2
* Custom ReAct loop (ไม่ใช้ LangGraph)
* Package/dependency management: uv

## D2. LLM: OpenAI-compatible Completions ผ่าน Ollama Cloud (Q2)

* LLM Gateway ใช้ **OpenAI-compatible Chat Completions API** โดย config `base_url` ชี้ไป Ollama cloud (เปลี่ยน provider ได้ด้วย config ไม่ต้องแก้โค้ด)
* ใช้ **native tool-calling format ของ OpenAI spec** เป็น interface กลางของ runtime
* ข้อควรระวัง: tool-calling support ขึ้นกับ model ที่รันบน Ollama — Gateway ต้องมี capability check และ error ชัดเจนเมื่อ model ไม่รองรับ tools
* Token accounting อ่านจาก `usage` ใน response; ถ้า provider ไม่ส่งให้ ใช้ tokenizer ประมาณ

## D3. Infrastructure: Full stack ตั้งแต่ MVP (Q4)

* **PostgreSQL** — durable state, skills, audit (source of truth)
* **Redis** — distributed lock (พร้อม fencing ด้วย version column ใน PG), cache, pub/sub สำหรับ streaming
* **RabbitMQ** — job queue สำหรับ agent runs และ scheduled jobs
* Docker Compose ประกอบทั้งสามตัว + API + worker ตั้งแต่ Phase 0
* หลักการยังคงเดิม: Redis/RabbitMQ **ไม่ใช่** durable source of truth — run state ทุกอย่าง recover ได้จาก PG อย่างเดียว

## D4. Tools ผ่าน MCP over HTTP เท่านั้น (Q6)

* MCP servers มีอยู่แล้วภายนอก — harness แค่ **register MCP ด้วย HTTP endpoint** ได้ก็พอ
* Tool Registry = ตาราง `mcp_servers` (name, url, auth header, enabled, health) + tool list ที่ discover จากแต่ละ server
* **ไม่ทำ stdio transport** — ตัดปัญหา stateful process บน stateless worker (ปิด gap G4)
* Credential ของ downstream systems (Elastic/GitLab/Email) เป็นความรับผิดชอบของ MCP server แต่ละตัว ไม่อยู่ใน harness (ปิด gap G9 สำหรับ MVP)
* Native tools ใน harness เหลือขั้นต่ำ: `finish` และ tool ภายในที่จำเป็นต่อ loop เท่านั้น

## ผลกระทบต่อ Task Breakdown (จาก review-open-questions.md §4)

* Phase 0: Docker Compose = PG + Redis + RabbitMQ ครบตั้งแต่แรก
* Phase 1 ข้อ 5: LLM Gateway = OpenAI-compatible client (`base_url` configurable), tool-calling capability check
* Phase 1 ข้อ 9: native tools ทดสอบ loop ให้น้อยที่สุด — hello/echo พอ เพราะ tool จริงมาจาก MCP
* Phase 2 ข้อ 11–12: ตัด HTTP API tool / Database tool adapter ออก — เหลือ **MCP HTTP adapter + registry** เป็นทางเดียว
* Q10 (MCP connection strategy) ตอบแล้ว: HTTP-only, connect ต่อ call หรือ pool ต่อ worker ตาม performance จริง

## ค่า default ที่ถือว่าตัดสินใจแล้ว (conventional)

* Q5: ใส่ `tenant_id` ในทุกตารางตั้งแต่ migration แรก แม้ยังใช้ tenant เดียว
* Q11: ใช้ explicit `finish` tool เป็น completion mechanism

---

# Decisions รอบสอง — ปิด gap และคำถามที่เหลือทั้งหมด

## D5. Concurrency / crash recovery (G1)

* `agent_runs` มี **`version` column (optimistic locking)** — ทุก checkpoint write ต้อง match version เดิม ไม่งั้น abort (fencing กัน worker ซ้อน)
* Worker ถือ **lease** บน run (`lease_owner`, `lease_expires_at` ใน PG) และ heartbeat ต่ออายุระหว่างทำงาน; Redis lock ใช้เป็น fast-path เท่านั้น ความถูกต้องอยู่ที่ PG
* มี **reaper** (background task) กวาด run ที่สถานะ `Running` แต่ lease หมดอายุ → requeue ให้ worker อื่น resume จาก checkpoint ล่าสุด

## D6. Tool execution idempotency (G2)

* **Write-ahead**: insert `tool_executions` สถานะ `started` (พร้อม idempotency key = `run_id + step_no + call_index`) **ก่อน** เรียก tool จริง แล้วค่อย update เป็น `succeeded/failed`
* Recovery policy ตาม risk tier ของ tool:
  * `ReadOnly` → retry อัตโนมัติ
  * `LowRiskWrite` → retry ได้ถ้า tool ประกาศ idempotent, ไม่งั้น mark `unknown` ให้ LLM ตรวจสอบผลก่อนทำต่อ
  * `SensitiveWrite` / `Destructive` → ห้าม auto-retry, เข้าสถานะ `WaitingApproval` ให้คนตัดสิน

## D7. ไม่มี streaming — ยึด stateless เต็มรูปแบบ (G3, Q3, Q9)

* **ตัด token streaming ออกจากระบบทั้งหมด** ยอมเสีย UX ส่วนนี้เพื่อความเรียบง่ายของ stateless worker
* Channel หลัก: **Slack** (คุยกับ agent ผ่าน Slack thread) + **Web UI** (ดูคำตอบ, run detail, approve)
* Web/API ใช้ **request → poll status → get result**; Slack ได้คำตอบเมื่อ run จบหรือ agent ต้องการ input/approval (worker post กลับผ่าน Slack API)

## D8. เลื่อน self-learning + evaluation pipeline ออกจาก MVP (G11, Q18)

* ไม่ build evaluation pipeline ใน MVP แต่**เก็บ `skill_usage` + run outcome ตั้งแต่วันแรก**
* Skill lifecycle ใน MVP = manual: คนสร้าง/แก้ revision, คน approve/publish ผ่าน API/UI
* เมื่อกลับมาทำ: assertion-based test cases ก่อน, LLM-as-judge ทีหลัง, minimum sample ≥ 20 runs ต่อ skill ก่อน propose revision

## คำตอบ P1 ที่เหลือ

| # | คำตอบ |
|---|---|
| Q7 | Checkpoint ทุก step: 1 step = 1 LLM call + tool calls ที่ตามมา persist ครบก่อนเริ่ม step ถัดไป; หน่วย resume = ก่อน LLM call ถัดไป |
| Q8 | ตาม D6 (write-ahead + policy ตาม risk tier) |
| Q9 | ไม่มี streaming ตาม D7 |
| Q10 | MCP over HTTP, เชื่อมต่อแบบ per-call ก่อน (เรียบง่าย) — ทำ connection pool ต่อเมื่อ latency เป็นปัญหาจริง |
| Q12 | Rolling summary: เมื่อ context เกิน ~70% ของ budget ให้ summarize step เก่าเก็บเป็น `summary` step ใน `agent_steps` และคง N step ล่าสุด (default 10) แบบ verbatim |
| Q13 | Skill selection: inject skill catalog (name + description) ใน system prompt แล้วให้ LLM เรียก meta-tool `use_skill(name)` เพื่อโหลด instruction เต็ม (progressive disclosure กับ skill ด้วย) |
| Q14 | Tool result: rule-based truncation อย่างเดียว (size limit + เก็บ head/tail) — ไม่มี LLM summarize step ใน MVP |
| Q15 | Approval ผ่าน Web UI เป็นหลัก + แจ้งใน Slack (ปุ่ม approve บน Slack เป็น enhancement); pending approval expire ที่ 24 ชม. → run เป็น `TimedOut` |
| Q16 | Harness เก็บแค่ MCP auth headers → encrypted column ใน PG (Fernet, key จาก env) พอสำหรับ MVP |
| Q17 | Budget ต่อ run (max steps/tokens เป็น default + override ได้) และ quota token รายวันต่อ tenant บันทึกใน `usage_records` |

## คำตอบ P2

| # | คำตอบ |
|---|---|
| Q18 | ตาม D8 — เลื่อน, เก็บข้อมูลก่อน |
| Q19 | Memory consolidation เลื่อนหลัง MVP; MVP มีแค่ conversation memory (messages ใน PG); ตอนทำจริงใช้ pgvector |
| Q20 | Scheduler = APScheduler ใน process แยก + Redis lock leader election (instance เดียว active) publish job เข้า RabbitMQ; ย้ายไป Temporal เมื่อจำเป็นจริง |
| Q21 | Slack เข้ามาใน MVP ตาม D7: Slack Events webhook → gateway, mapping `channel_id + thread_ts` ↔ conversation; LINE/Telegram ทีหลัง |
| Q22 | Sub-agents / multi-agent เลื่อนไม่มีกำหนด จนกว่า single agent จะเสถียร |
