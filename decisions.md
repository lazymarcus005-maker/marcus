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

* Q3: Channel แรก = REST API + Simple Web UI (Slack/LINE ทีหลัง)
* Q5: ใส่ `tenant_id` ในทุกตารางตั้งแต่ migration แรก แม้ยังใช้ tenant เดียว
* Q11: ใช้ explicit `finish` tool เป็น completion mechanism
