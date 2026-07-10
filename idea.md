# สรุประบบ AI Agent Harness แบบ Hermes

ระบบนี้คือ **AI Agent Runtime แบบ stateless service** ที่ทำงานแบบคิดไปทำไป เรียก Tool/MCP แล้วใช้ผลลัพธ์จริงมาตัดสินใจต่อ พร้อมรองรับ persistent state, skills, scheduling, memory และ self-learning

## High-level architecture

```text
Channels
Web / Slack / LINE / Telegram / API
                ↓
Agent Gateway
Auth / Rate Limit / Streaming / Routing
                ↓
Agent Runtime
├── ReAct Loop
├── Context Builder
├── Skill Selector
├── Progressive Tool Discovery
├── Policy / Guardrails
├── State Manager
└── Completion Detector
        ↓               ↓
    LLM Gateway     Tool Executor
                        ↓
               MCP / Native Tools
                        ↓
        Elastic / GitLab / Email / DB / APIs
```

Infrastructure:

```text
PostgreSQL = durable state และ skills
Redis      = cache, lock, coordination
Queue      = background jobs
Scheduler  = scheduled agent runs
Observability = logs, traces, metrics, audit
```

---

## 1. Agent Gateway

รับข้อความจากหลายช่องทางและแปลงเป็น contract กลาง

หน้าที่หลัก:

* Authentication และ authorization
* Rate limiting
* Session routing
* Streaming response
* Webhook handling
* สร้างหรือ resume agent run

ตัว Gateway และ Worker สามารถ scale หลาย replicas ได้ เพราะไม่เก็บ state สำคัญใน memory ของ instance

---

## 2. Agent Runtime

เป็นหัวใจของระบบ ทำหน้าที่ควบคุม lifecycle ของงานทั้งหมด

```text
Receive request
→ Load persistent state
→ Load context / memory / skills
→ Discover relevant tools
→ Call LLM
→ Execute tool
→ Store observation
→ Call LLM again
→ Complete / wait / fail
```

Runtime ไม่ควรเป็น workflow ตายตัว แต่เป็น execution engine ที่ให้ LLM ตัดสินใจจากหลักฐานล่าสุด

---

## 3. ReAct Agent Loop

รูปแบบการทำงานคือ:

```text
Reason
→ Act
→ Observe
→ Reason again
```

ตัวอย่าง:

```text
ผู้ใช้ให้ request ID
→ Agent ค้น Elastic
→ พบ downstream timeout
→ Agent ค้น trace เพิ่ม
→ พบ service ต้นเหตุ
→ Agentอ่าน API inventory
→ สรุป root cause พร้อมหลักฐาน
```

LLM มีหน้าที่เลือก action ถัดไป ส่วน Harness มีหน้าที่ตรวจสอบและดำเนินการอย่างปลอดภัย

```text
LLM decides
→ Harness validates
→ Tool executes
→ Observation persisted
→ LLM continues
```

---

## 4. Progressive Tool Disclosure

ไม่ควรส่ง Tools ทั้งหมดพร้อม full schema เข้า LLM ตั้งแต่แรก

ควรเปิดเผยเป็นลำดับ:

```text
Level 1: Domain
elastic / gitlab / database / email

Level 2: Tool summary
search_logs / get_trace / query_database

Level 3: Full schema
parameters / constraints / examples
```

Component นี้อยู่ใน:

```text
Agent Runtime
└── Tool Discovery
    ├── Domain Selector
    ├── Tool Selector
    └── Schema Loader
```

ข้อดี:

* ลด token
* ลด context noise
* ลดการเลือก tool ผิด
* รองรับ MCP จำนวนมากขึ้น

---

## 5. Tool Registry และ Tool Executor

Tools ทุกประเภทควรถูกแปลงเป็น abstraction เดียวกัน

```text
MCP Tool
Native Tool
HTTP API Tool
Database Tool
GitLab Tool
Elastic Tool
```

Tool Executor ต้องตรวจ:

```text
Tool มีอยู่จริง
Agent มีสิทธิ์ใช้
User มีสิทธิ์ใช้
Arguments ตรง schema
ต้อง approval หรือไม่
ยังไม่เกิน quota หรือ timeout
```

ผลลัพธ์ Tool ต้องผ่าน:

```text
Normalize
→ Truncate
→ Summarize
→ Persist
→ Append เข้า context
```

ไม่ควรส่ง raw result ขนาดใหญ่กลับเข้า LLM โดยตรง

---

## 6. Guardrails

Harness ต้องคุม execution ไม่ให้ LLM ทำงานแบบไร้ขอบเขต

ค่าที่ต้องมี:

```text
Max steps
Max tool calls
Timeout
Token budget
Repeated tool-call detection
Cancellation
Retry limit
Permission
Approval
Result size limit
```

สถานะของ run:

```text
Pending
Running
WaitingUserInput
WaitingApproval
Completed
Failed
Cancelled
TimedOut
```

---

## 7. Persistent State

ระบบสามารถทำเป็น **stateless service** ได้ แต่ตัวระบบยังต้องมี persistent state

```text
API / Worker instance = Stateless
PostgreSQL             = Stateful source of truth
```

State ที่ควรเก็บ:

```text
Agent runs
Messages
Current step
Goal
Tool calls
Observations
Active skill revision
Token / step budget
Approval status
Errors / retries
Scheduled jobs
Final result
```

ตารางหลัก:

```text
agent_runs
agent_messages
agent_steps
tool_executions
approval_requests
scheduled_jobs
usage_records
```

Flow:

```text
Load state from DB
→ Execute one or more agent turns
→ Persist checkpoint
→ Any worker can resume
```

Redis ใช้สำหรับ:

```text
Distributed lock
Short-lived cache
Rate limiting
Active-run coordination
```

Redis ไม่ควรเป็น durable source of truth

---

## 8. Skill System

Skill คือแนวทางการตัดสินใจและความรู้ในการทำงาน ไม่ใช่ script ที่กำหนดทุก step ตายตัว

ตัวอย่าง Skill:

```text
Investigate Elastic Error
Analyze API Impact
Summarize Email
Review Source Code
```

Skill อาจประกอบด้วย:

```text
Instruction
Description
Input schema
Output schema
Required tools
Constraints
Examples
Test cases
Risk policy
```

สำหรับระบบนี้เลือกให้ **Database เป็น source of truth ของ Skill** เพราะต้องการ:

* Stateless deployment
* Dynamic loading
* Agent-generated skills
* Ranking และ permission
* Self-learning
* Runtime update โดยไม่ rebuild container

---

## 9. Skill Database Design

ไม่ควรแก้ Skill record เดิมทับ แต่ต้องใช้ immutable revisions

```text
skills
├── id
├── name
├── description
├── active_revision_id
├── status
└── ownership

skill_revisions
├── id
├── skill_id
├── version
├── instruction
├── manifest_json
├── input_schema_json
├── output_schema_json
├── required_tools_json
├── change_reason
├── created_from_run_id
└── created_at

skill_evaluations
├── revision_id
├── test_case
├── score
├── passed
└── details

skill_usage
├── revision_id
├── run_id
├── success
├── latency
├── token_usage
└── feedback
```

สถานะ Skill:

```text
Draft
→ Evaluating
→ Approved
→ Published
→ Deprecated
```

Published revision ต้อง immutable และ rollback ได้

---

## 10. Self-learning

Agent ห้ามแก้ Published Skill โดยตรง

Learning flow:

```text
Agent runs
→ Collect outcomes and feedback
→ Detect repeated success/failure patterns
→ Generate skill proposal
→ Create draft revision
→ Run automated evaluation
→ Compare with active revision
→ Approve
→ Publish
→ Update active_revision_id
```

Guardrails ที่จำเป็น:

```text
ห้าม Agent publish ตัวเองทันที
ต้องมี minimum sample size
ต้องผ่าน regression tests
ห้ามเพิ่ม tool permission เอง
ต้องเก็บ source run
ต้อง rollback ได้
ต้องแยก tenant และ environment
```

Git ยังใช้ได้ในฐานะ:

```text
Export
Backup
Audit artifact
Human review
Environment promotion
```

แต่ runtime source of truth คือ Database

---

## 11. Memory

Memory ควรแยกเป็น:

```text
Working memory
Conversation memory
User memory
Organizational knowledge
```

การเขียน long-term memory ควรผ่าน policy:

```text
Agent proposes memory
→ Validate
→ Deduplicate
→ Classify
→ Store with source and confidence
```

ไม่ควรให้ Agent เขียน memory แบบอิสระทุกอย่าง

---

## 12. Scheduler และ Worker

Scheduled tasks ใช้ Agent Runtime ตัวเดียวกับ interactive requests

```text
Scheduler
→ Publish job
→ Queue
→ Worker
→ Load state
→ Run Agent Runtime
```

ตัวอย่าง:

```text
สรุป email ทุกเช้า
ตรวจ error rate ทุกชั่วโมง
สร้างรายงานทุกวันจันทร์
แจ้งเตือนเมื่อพบเหตุผิดปกติ
```

---

## 13. Permission และ Approval

Tools ควรถูกจัดระดับความเสี่ยง:

```text
ReadOnly
LowRiskWrite
SensitiveWrite
Destructive
```

ตัวอย่าง:

```text
Elastic search      = ReadOnly
Create GitLab issue = LowRiskWrite
Send email          = SensitiveWrite
Delete resource     = Destructive
```

Permission ต้องตรวจที่ application layer ไม่ใช่พึ่ง system prompt

---

## 14. Observability

ทุก agent run ต้อง trace ได้ครบ:

```text
Request
Context building
Skill selection
Tool discovery
LLM calls
Tool calls
Tool results
Approval
Token usage
Latency
Errors
Final response
```

Metrics สำคัญ:

```text
Active runs
Run duration
Steps per run
Tool calls per run
Tool failure rate
Duplicate calls
Token usage
Waiting approvals
Scheduler failures
Skill success rate
```

---

# Tech Stack ที่เหมาะ

## ตัวเลือกหลัก

สำหรับความยืดหยุ่นด้าน Agent และ self-learning:

```text
Python
FastAPI
Pydantic
Custom ReAct Loop หรือ LangGraph
Official MCP Python SDK
PostgreSQL
Redis
RabbitMQ
Quartz equivalent เช่น APScheduler หรือ Temporal
OpenTelemetry
React
Docker
```

สำหรับความแข็งแรงด้าน Enterprise และ ecosystem เดิมของคุณ:

```text
.NET 10
ASP.NET Core
Microsoft.Extensions.AI
Custom ReAct Loop
Official MCP C# SDK
PostgreSQL
Redis
RabbitMQ
Quartz.NET
OpenTelemetry
React
Docker
```

Go เหมาะกับ:

```text
Gateway
MCP proxy
High-concurrency worker
Webhook receiver
Streaming infrastructure
```

แต่ไม่ใช่ตัวเลือกดีที่สุดสำหรับ agent logic ที่ต้อง iterate และทดลองบ่อย

---

# ข้อแนะนำด้านภาษา

ทางเลือกที่บาลานซ์:

```text
Option A: Python ทั้งระบบ
เหมาะกับ MVP และ AI experimentation

Option B: .NET ทั้งระบบ
เหมาะกับ Enterprise integration และทีมเดิม

Option C: .NET/Go Gateway + Python Agent Runtime
ยืดหยุ่นที่สุด แต่มี operational complexity สูงกว่า
```

สำหรับ MVP คนเดียว ผมแนะนำ:

```text
Python + FastAPI
Custom ReAct Loop
Pydantic
Official MCP SDK
PostgreSQL
Redis
React
Docker Compose
```

แต่ถ้าต้องเชื่อมระบบ .NET เดิมจำนวนมากและทีมจะดูแลต่อ:

```text
.NET 10 + Custom Agent Runtime
```

---

# MVP Components

ควรเริ่มจาก:

```text
1. Agent API
2. Persistent Run State
3. Custom ReAct Loop
4. LLM Gateway
5. Tool Registry
6. MCP Adapter
7. Progressive Tool Disclosure
8. Skill Registry in PostgreSQL
9. Immutable Skill Revisions
10. Policy / Guardrails
11. Audit / Tracing
12. Simple Web UI
```

ยังไม่ต้องเริ่มจาก:

```text
Multi-agent orchestration
Fully autonomous self-learning
Vector search สำหรับ tools
Kubernetes
Complex distributed workflows
```

ให้ทำ Single Agent Runtime ให้เสถียรก่อน แล้วค่อยเพิ่ม Scheduler, Multi-channel, Memory consolidation, Evaluation Pipeline และ Sub-agents

## แกนของระบบทั้งหมด

```text
LLM ตัดสินใจว่าจะทำอะไรต่อ
Harness ควบคุมว่าจะอนุญาตและ execute อย่างไร
Tool ทำงานจริง
Observation ถูกบันทึกและส่งกลับให้ LLM
State และ Skills อยู่ใน Database
Runtime ทุก instance เป็น Stateless
Skill ใหม่ต้องผ่าน Evaluation ก่อน Publish
```
