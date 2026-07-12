# Harness Reliability, Safety, and Reasoning Improvement Plan

เอกสารนี้เป็นผลจากการ review โค้ดปัจจุบันทั้ง `harness/`, `marcus_code/`, API,
worker, LLM gateway, tool execution, MCP, skills, context compaction และ tests โดยมี
เป้าหมายให้ระบบ:

- ปลอดภัยโดยอาศัย runtime enforcement มากกว่า prompt
- ทำงานถูกต้องและอธิบายเหตุผลได้
- ใช้ token, เวลา, network และ tool calls อย่างประหยัด
- ลด hallucination โดยเฉพาะเมื่อใช้โมเดลระดับล่างหรือระดับกลาง
- ฟื้นตัวได้เมื่อ provider หรือ tool มีปัญหา แต่ไม่ retry จนเกิด loop
- ตรวจสอบย้อนหลังได้ว่า “ระบบสรุปว่าสำเร็จจากหลักฐานอะไร”

เอกสารนี้ไม่เสนอ multi-agent ในระยะใกล้ เพราะ single-agent runtime ยังลดความเสี่ยงและ
ต้นทุนได้อีกมาก การเพิ่ม agent หลายตัวก่อนแก้ control plane จะเพิ่มทั้ง hallucination,
token cost และ failure surface

---

## 1. Executive assessment

### จุดแข็งที่ควรรักษา

1. Server harness มี durable run state, optimistic version fencing, lease heartbeat และ
   stale-run reaping อยู่แล้ว (`runtime/repository.py`, `lease.py`, `reaper.py`)
2. Tool execution ใช้ write-ahead record และ idempotency key ก่อนเรียก external tool
   (`runtime/tool_executor.py`) ซึ่งเป็นฐาน crash recovery ที่ถูกต้อง
3. มี risk tiers และ approval gate แยกจาก LLM
4. มี progressive disclosure สำหรับ MCP tools และ skills ช่วยลด tool schema tokens
5. มี run/token/tool/time budgets, tenant quota และ audit records
6. Marcus CLI มี workspace scoping, secret redaction, SSRF protection, process-tree
   cleanup, bounded retries, context meter และ background-process lifecycle
7. Test suite ครอบคลุม behavior สำคัญจำนวนมากและใช้ fakes ได้ดี

### ข้อสรุปสำคัญ

ปัญหาหลักไม่ใช่ “prompt ยังไม่เก่งพอ” แต่เป็นการที่ runtime ยังปล่อยให้โมเดลตัดสินใจ
หลายเรื่องที่ควรเป็น deterministic policy เช่น:

- งานนี้ต้องอ่านอะไรขั้นต่ำก่อนแก้
- tool arguments ถูก schema และปลอดภัยหรือไม่
- tool failure แบบใด retry ได้
- หลักฐานใดเพียงพอที่จะประกาศว่าสำเร็จ
- เมื่อใดถือว่าไม่มี progress และต้องหยุด
- summary/compaction เก็บ facts ครบหรือไม่

สำหรับโมเดลเล็ก ควรลดอิสระของโมเดลแล้วเพิ่ม state machine, typed outcomes,
verification gates และ deterministic routing แทนการเพิ่ม system prompt ยาวขึ้น

---

## 2. Priority findings

| Priority | Finding | ผลกระทบ |
|---|---|---|
| P0 | ไม่มี evidence gate ก่อน `finish` หรือ final answer | โมเดลอาจประกาศว่าสำเร็จทั้งที่ build/test/tool ล้มเหลว |
| P0 | Tool arguments ไม่ถูก validate ด้วย JSON Schema ก่อน handler | โมเดลเล็กส่ง type/field ผิด ทำให้เสีย step และเกิด retry loop |
| P0 | Server `run_cli` ยังฆ่าเพียง shell process ต่างจาก CLI ที่ฆ่า process tree | child process และ pipe อาจค้าง worker/event loop |
| P0 | Native tools ของ server และ CLI เป็น implementation คนละชุด | security/reliability fixes drift และ behavior ไม่สอดคล้อง |
| P0 | Production config มีค่า `changeme` และ legacy auth behavior | เสี่ยง deploy แบบ fail-open หรือใช้ encryption key อ่อน |
| P1 | Compaction เทียบกับ cumulative token budget ไม่ใช่ model context window | compact ช้า/เร็วผิดจังหวะ และ context overflow ได้ |
| P1 | LLM summary เป็น source of truth โดยไม่มี fact preservation check | summary hallucination ทำลาย context สำหรับ step ถัดไป |
| P1 | MCP names ใช้ global tool name และ `setdefault` | tool ชื่อชนกันข้าม server อาจเลือก/เรียกผิด domain |
| P1 | Text จาก file/web/MCP ถูกส่งกลับเป็น tool message โดยไม่มี trust label | prompt injection จากข้อมูลภายนอกมีโอกาสเปลี่ยนพฤติกรรมโมเดล |
| P1 | Retry policy กระจายอยู่ gateway, CLI loop และ tool executor | latency/cost สะสม และคาดการณ์จำนวน attempt ยาก |
| P1 | `max_steps=100` และ CLI token budget default unlimited | โมเดลระดับล่างสามารถเผา token จำนวนมากก่อน circuit breaker |
| P1 | Batch tool calls รันตามลำดับโดยไม่วิเคราะห์ dependency | โมเดลเล็กมักสร้าง call ที่สองโดยอาศัยผล call แรกที่ยังไม่รู้ |
| P2 | Usage ที่ provider ไม่ส่งถูกนับเป็น 0 | budget/quota/observability fail-open |
| P2 | Compaction LLM calls ไม่ถูกรวม cost ใน run usage อย่างครบถ้วน | ค่าใช้จ่ายจริงสูงกว่าที่รายงาน |
| P2 | Ollama browser storage state เป็น plaintext secret material | local account/session theft หากเครื่องหรือ home directory ถูกอ่าน |
| P2 | Playwright เป็น core dependency เพื่อฟีเจอร์ `/usage` | install ใหญ่ขึ้นและเพิ่ม supply-chain/runtime surface |
| P2 | Full integration tests พึ่ง RabbitMQ ภายนอกและไม่ hermetic | CI signal ไม่แน่นอนและแยก code failure จาก infra failureยาก |

---

## 3. Target execution model

เปลี่ยนจาก ReAct แบบอิสระ:

```text
LLM → tool → LLM → tool → final
```

เป็น state machine ที่ runtime เป็นเจ้าของ:

```text
INTAKE
  → PLAN
  → PREFLIGHT
  → ACT
  → OBSERVE
  → VERIFY
  → COMMIT / REPLAN / ASK_USER / FAIL
```

### 3.1 Task contract

ก่อนอนุญาต mutation ให้สร้าง `TaskContract` แบบ typed:

```json
{
  "objective": "ทำให้ endpoint convert ทำงาน",
  "constraints": ["ห้ามออกนอก workspace"],
  "deliverables": ["source change", "passing build", "HTTP evidence"],
  "verification": [
    {"kind": "command", "command_class": "build", "required": true},
    {"kind": "http", "expected_status": 200, "required": true}
  ],
  "allowed_write_scope": ["src/**", "tests/**"]
}
```

โมเดลเสนอ contract ได้ แต่ runtime validate และเติม defaults ตาม task class

### 3.2 Plan as data, not prose

เก็บ plan เป็นรายการ step ที่มี state:

```text
pending → running → verified → completed
                    ↘ failed → replanned
```

แต่ละ step ต้องระบุ:

- intent
- required observations
- allowed tools
- expected outcome
- verifier
- maximum attempts

โมเดลเล็กจะเห็นเฉพาะ current step + facts ที่จำเป็น ไม่ต้องแบก plan prose ยาวทุก call

### 3.3 Evidence ledger

สร้าง `EvidenceRecord` จาก tool results โดย runtime:

```json
{
  "claim": "build passes",
  "source": "tool_execution:uuid",
  "status": "supported",
  "facts": {"exit_code": 0, "command_class": "build"},
  "observed_at": "..."
}
```

`finish` ต้อง reject หาก required evidence ขาด, stale หรือขัดแย้งกัน โมเดลจึงไม่สามารถ
กล่าวว่า “หยุด service แล้ว” หาก `stop_process` คืน error

---

## 4. ลด hallucination สำหรับโมเดลระดับล่าง–กลาง

### 4.1 Capability profile ต่อ model

เพิ่ม model profile แทนใช้ behavior เดียวทุก model:

| Tier | Tool calls/step | Visible tools | Planning | Verification |
|---|---:|---:|---|---|
| low | 1 | 3–6 | runtime template | mandatory deterministic |
| medium | 1–2 independent calls | 6–12 | typed plan | mandatory |
| high | bounded batch | progressive | model-assisted | mandatory |

ห้าม infer tier จากชื่อ model อย่างเดียว ให้ config หรือผ่าน capability probe/evaluation

### 4.2 One dependent action per turn

สำหรับ low/medium model ให้ยอมรับ tool call เดียวต่อ LLM responseโดย default หากมีหลาย call:

- รันพร้อมกันได้เฉพาะ read-only calls ที่ประกาศว่า independent
- หาก call B ต้องใช้ output ของ call A ให้ reject B ด้วย error code
  `DEPENDENCY_REQUIRES_NEW_STEP`
- mutation calls ทำทีละรายการ

วิธีนี้ลด argument guessing และลด partial-success ambiguity

### 4.3 Typed tool observations

ทุก tool result ควรอยู่ใน envelope เดียว:

```json
{
  "ok": true,
  "code": "COMMAND_SUCCEEDED",
  "summary": "Build completed",
  "facts": {"exit_code": 0},
  "artifacts": [],
  "retryable": false,
  "redactions": 0
}
```

ห้ามให้ LLM ตีความจาก free-form `stdout` เพียงอย่างเดียว Runtime adapter ต้อง extract
exit code, HTTP status, file hash, test count และ process status เป็น facts

### 4.4 Schema validation and bounded repair

ก่อน handler:

1. validate arguments ด้วย JSON Schema
2. normalize safe coercions เช่น `"30"` → `30` เฉพาะ schema ที่อนุญาต
3. หากผิด ส่ง concise validation error พร้อม fields ที่ผิด
4. ให้โมเดล repair ได้หนึ่งครั้ง
5. ผิดซ้ำ → ask user หรือ fail step

อย่าส่ง Python traceback หรือ exception text ยาวกลับโมเดล

### 4.5 Claim policy

Final response ใช้ facts จาก evidence ledger ไม่ใช่ความจำของโมเดล:

- ตัวเลข, paths, status และ test totals ต้อง copy จาก evidence
- unsupported claim ถูกตัดหรือทำเครื่องหมาย “ยังไม่ยืนยัน”
- final answer generator รับเฉพาะ approved facts
- หากใช้โมเดลเล็ก สามารถใช้ deterministic template โดยไม่เรียก LLM เพิ่ม

---

## 5. Safety improvements

### 5.1 Fail-closed production startup

เมื่อ `env != development` ให้ process ปฏิเสธ startup หาก:

- `secret_key` ยังเป็น default
- LLM/API/Slack secrets เป็น placeholder
- encryption key สั้นหรือไม่มี key version
- CORS/host config กว้างเกิน policy
- legacy unauthenticated tenant mode ยังเปิด

เพิ่ม `/readyz` check สำหรับ unsafe configuration โดยไม่เปิดเผย secret

### 5.2 Unify native tool implementations

ย้าย shared implementation ไป package เช่น `harness.tools.local` แล้วให้ CLI/server inject:

- root scope
- approval policy
- process manager
- output policy
- network policy

ต้องมี contract tests ชุดเดียวรันกับทั้งสอง adapters ป้องกันกรณี CLI แก้ process-tree kill
แต่ server `runtime/native_tools.py` ยังใช้ `proc.kill()` กับ shell อย่างเดียว

### 5.3 Command policy before shell

แยก command execution ออกจาก raw shell string:

- prefer argv execution (`create_subprocess_exec`) สำหรับ known commands
- shell mode ต้อง explicit และ approval สูงกว่า
- classify command: inspect/build/test/network/package/git/db/system
- allow workspace command policy ต่อ mode/tenant
- block command substitution, redirects ออกนอก workspace และ environment secret dumps
- record executable, argv, cwd และ normalized command hash

### 5.4 File safety

- Server `read_file` ต้องใช้ secret redaction เทียบเท่า CLI
- ทุก write/edit เก็บ pre-image hash และ post-image hash
- edit ต้องใช้ expected hash ป้องกันเขียนทับ concurrent user changes
- จำกัด total bytes written ต่อ run
- atomic write (`temp + fsync + replace`) สำหรับ config/artifacts สำคัญ
- symlink/reparse-point checks ต้องเกิดทั้งก่อนและทันทีตอนเปิด file เพื่อลด TOCTOU

### 5.5 Network/MCP safety

- ใช้ canonical tool id: `server_id:tool_name` ไม่ใช้ name เดี่ยว
- validate MCP tool schemas และ cap schema/description size ก่อนเข้า prompt
- pin resolved IP ต่อ request หรือใช้ controlled egress proxy ป้องกัน DNS rebinding
- timeout แยก connect/read/write และ overall deadline
- treat MCP/file/web content เป็น untrusted data พร้อม trust metadata
- ห้าม tool result สั่ง `load_tool`, เปลี่ยน mode หรือแก้ plan โดยตรง

### 5.6 Local credential safety

`ollama-storage-state.json` มี bearer/session cookies ไม่ควรพึ่ง `chmod` อย่างเดียวบน Windows:

- ใช้ Windows DPAPI / macOS Keychain / Secret Service
- เก็บ encryption metadata และ key version
- `/usage logout` ลบ storage state, cache และ browser profile
- ตั้งอายุ cache และแสดง last refreshed
- ย้าย Playwright เป็น optional extra เช่น `marcus[ollama-usage]`

---

## 6. Reliability and anti-loop design

### 6.1 Central retry budget

สร้าง `RetryController` จุดเดียวสำหรับ LLM, MCP และ tools:

```text
max attempts per operation
max retry delay
overall deadline
retryable error codes
cost already spent
idempotency classification
```

ห้าม retry ซ้อนหลาย layer เช่น gateway retry + loop fallback + worker retry โดยไม่มี shared
budget ทุก attempt ต้องมี `attempt_id` และ reason

### 6.2 Progress/stagnation detector

แทนการตรวจ identical call อย่างเดียว ให้สร้าง progress fingerprint จาก:

- files/facts/evidence ที่เพิ่ม
- error code ล่าสุด
- plan step state
- normalized tool + arguments
- workspace diff hash

หาก N steps ไม่มี evidence ใหม่หรือ diff ใหม่ ให้:

1. ลด tool set
2. ขอ replan แบบ structured หนึ่งครั้ง
3. หากยัง stagnate ให้ ask user/fail อย่างมีเหตุผล

### 6.3 Error taxonomy

แทน free-form strings ด้วย error codes:

```text
INVALID_ARGUMENT
POLICY_DENIED
AUTH_REQUIRED
TRANSIENT_NETWORK
RATE_LIMITED
TIMEOUT
PROCESS_EXITED
OUTCOME_UNKNOWN
VERIFICATION_FAILED
NO_PROGRESS
```

แต่ละ code มี retry policy ที่ runtime กำหนด โมเดลทำหน้าที่เลือกทางเลือก ไม่ตัดสินว่า error
retryable เอง

### 6.4 Cleanup semantics

- resource ทุกชนิดต้องเป็น session/turn scoped และ implement `aclose`
- process, browser, HTTP client, MCP session และ prompt UI ต้องอยู่ใน async context stack
- background process ต้องมี ownership record และ cleanup deadline
- cleanup failure ต้องแสดงเป็น evidence ไม่ถูกกลืน
- เพิ่ม leak tests บน Windows Proactor และ POSIX

---

## 7. Context and cost control

### 7.1 แยกสาม budget

อย่าใช้ `run.token_budget` ตัวเดียวสำหรับทุกความหมาย:

1. `context_window_tokens` — tokens ที่ส่ง request ปัจจุบัน
2. `run_token_budget` — tokens สะสมรวม LLM calls ทั้ง run
3. `cost_budget` — เงินหรือ provider quota

Compaction ต้องเทียบกับข้อ 1 ไม่ใช่ข้อ 2

### 7.2 Account for hidden calls

ต้องนับ usage ของ:

- compaction/summarization
- retries ที่ provider charge
- repair calls
- verification/finalization calls
- skill routing calls หากเพิ่มในอนาคต

หาก provider ไม่ส่ง usage ให้ใช้ estimate พร้อม `usage_source=estimated` และบังคับ conservative
budget ไม่ใช่นับ 0

### 7.3 Deterministic compaction first

ก่อนเรียก LLM summary:

- drop duplicate directory listings
- replace large stdout ด้วย artifact reference + facts
- keep only latest successful result ต่อ command class
- fold repeated errors เป็น count
- preserve decisions, constraints, file hashes และ evidence verbatim

ถ้าต้องใช้ LLM summary ให้ output เป็น schema และ validate facts กับ source records

### 7.4 Prompt and tool-schema budget

- มี token budget แยกสำหรับ system prompt, skills และ tool schemas
- rank tools deterministically และส่ง top-K
- description ต้องสั้นและใช้ examples เฉพาะ ambiguous fields
- cache stable prompt prefix หาก provider รองรับ
- ไม่ส่ง full workspace listing ซ้ำเมื่อ snapshot hash ไม่เปลี่ยน

### 7.5 Adaptive stopping

Default CLI ไม่ควรเป็น 100 steps + unlimited tokens สำหรับทุก model:

```text
low tier:    20–30 steps, strict one-call policy
medium tier: 40–50 steps
high tier:   configurable up to 100
```

ขยาย budget ได้เมื่อมี measurable progress ไม่ใช่ตามคำขอของโมเดล

---

## 8. Skill selection

แนวทาง catalog → LLM เลือก → `load_skill` เหมาะกับ CLI และควรรักษาไว้ แต่เพิ่ม safeguards:

- validate `SKILL.md` size, frontmatter และ allowed references
- mark skill content เป็น privileged local instruction เฉพาะเมื่อ trusted
- เก็บ skill digest ใน run audit
- จำกัดหนึ่ง active skill โดย default สำหรับ low-tier model
- หาก skill candidates ใกล้กัน ไม่ควรให้ lexical tie-break ตัดสิน silently
- required tools ต้องตรวจ risk/policy อีกครั้ง Skill ไม่สามารถยกระดับสิทธิ์
- วัด success rate แยกตาม model tier และ task class ป้องกันเลือก skill จาก aggregate ที่หลอกตา

ไม่ควรเพิ่ม LLM routing call แยกโดย default เพราะเพิ่ม cost การ lexical retrieval แบบ conservative
ที่มีอยู่เป็นฐานที่ดี

---

## 9. Observability and audit

เพิ่ม structured events:

```text
plan.created
policy.denied
tool.validation_failed
tool.retry_scheduled
evidence.recorded
verification.failed
context.compacted
stagnation.detected
resource.cleanup_failed
```

Metrics ที่ควรมี:

- success rate แบบ verified/unverified
- tokens และ cost ต่อ successful task
- tool calls ต่อ verified task
- retry amplification factor
- no-progress steps
- invalid argument rate แยกตาม model
- claim rejection rate จาก evidence gate
- compaction fact-loss rate
- leaked resource count

Log ต้อง redact secrets ก่อน serialization ไม่ใช่หวังว่า caller ไม่ส่ง secret

---

## 10. Testing and evaluation strategy

### 10.1 Contract tests

สร้างชุดเดียวสำหรับ CLI/server tool implementations:

- path traversal, symlink, reparse point
- secret redaction
- timeout + process tree cleanup
- output truncation
- malformed arguments
- cancellation at every await boundary

### 10.2 Model-tier simulation

เพิ่ม scripted weak-model behaviors:

- tool name hallucination
- wrong argument type
- repeated call with cosmetic argument changes
- claims success after error
- multiple dependent calls in one response
- ignores instruction to stop process
- produces empty final response

Expected result ต้องเป็น runtime repair/deny/verify ไม่ใช่เพียง prompt ทำให้ model ตอบดีขึ้น

### 10.3 Golden task suite

อย่างน้อย 20–50 tasks ต่อ class:

- explain-only
- single-file edit
- multi-file implementation
- build/test repair
- background service + HTTP verification
- destructive request requiring approval
- provider outage/recovery

วัด correctness จาก deterministic assertions และ artifacts ก่อนใช้ LLM-as-judge

### 10.4 Fault injection

จำลอง crash ที่จุด:

- หลัง write-ahead ก่อน tool call
- หลัง external side effect ก่อน result commit
- ระหว่าง lease heartbeat
- ระหว่าง compaction
- หลัง approval แต่ก่อน execution
- ขณะ process/browser cleanup

Full-stack tests ควรใช้ Testcontainers หรือ compose fixture และ skip พร้อมเหตุผลเมื่อ infra ไม่มี
ไม่ควรปล่อย connection refused เป็น test error ที่ปะปนกับ code regression

---

## 11. Recommended roadmap

### Phase 0 — Correctness baseline (P0)

1. เพิ่ม JSON Schema validation ก่อนทุก tool handler
2. เพิ่ม typed `ToolOutcome` และ error taxonomy
3. เพิ่ม evidence ledger + verification gate ก่อน finish
4. unify CLI/server native tool core
5. port process-tree cleanup ไป server `run_cli`
6. fail-closed production config validation

Acceptance criteria:

- final success ทุกครั้งมี evidence id
- invalid tool arguments ไม่ถึง handler
- child process leak tests ผ่านบน Windows/Linux
- default secrets ทำให้ production startup fail

### Phase 1 — Weak-model control plane

1. TaskContract + typed plan
2. model capability profiles
3. one dependent tool call per step
4. stagnation detector
5. bounded repair/replan policy
6. deterministic final response template สำหรับ low tier

Acceptance criteria:

- scripted weak model ที่ claim success หลัง tool errorไม่สามารถ finish ได้
- cosmetic retry loops หยุดภายใน configured bound
- invalid argument repair ไม่เกินหนึ่ง LLM call

### Phase 2 — Context and cost

1. แยก context/run/cost budgets
2. นับ hidden LLM calls
3. deterministic compaction + artifact references
4. adaptive step/tool budgets
5. prompt/tool-schema token accounting

Acceptance criteria:

- reported usage ไม่ต่ำกว่าการใช้งานจริงที่วัดได้
- p95 tokens ต่อ verified task ลดลงอย่างน้อย 25%
- context overflow เป็นศูนย์ใน golden suite

### Phase 3 — Security hardening

1. canonical MCP tool ids
2. content trust labels + injection resistance
3. command policy/argv execution
4. OS credential vault สำหรับ local browser state
5. optionalize Playwright
6. egress policy/proxy

Acceptance criteria:

- cross-domain tool collision เป็นไปไม่ได้
- untrusted tool output ไม่สามารถเปลี่ยน mode/policy
- secrets ไม่ปรากฏใน LLM messages/log snapshots ของ security suite

### Phase 4 — Evaluation and operations

1. golden tasks ใน CI
2. fault injection suite
3. model-tier scorecard
4. verified-success/cost dashboards
5. canary rollout ต่อ model/provider

---

## 12. Suggested configuration additions

```toml
[agent]
model_tier = "medium"
max_tool_calls_per_step = 1
max_argument_repairs = 1
max_replans = 2
max_no_progress_steps = 3
require_verified_finish = true

[budget]
context_window_tokens = 32768
run_token_budget = 50000
cost_budget_usd = 1.00
tool_schema_token_budget = 4000

[tools]
shell_enabled = false
max_write_bytes_per_run = 2000000
require_expected_file_hash = true

[security]
fail_on_default_secrets = true
legacy_auth_enabled = false
untrusted_content_policy = "data_only"
```

ค่า config ต้อง validate ความสัมพันธ์ เช่น compact target ต้องต่ำกว่า threshold และ token budgets
ต้องเป็นค่าบวก

---

## 13. Definition of “reasoned and correct”

งานหนึ่งถือว่าทำงานอย่างมีเหตุผลเมื่อ:

1. มี objective และ constraints ที่ชัด
2. ทุก mutation เชื่อมกับ plan step
3. tool arguments ผ่าน schema และ policy
4. retry เกิดจาก typed retryable error และอยู่ใน shared budget
5. ทุก conclusion สำคัญมี evidence
6. verification เป็น independent check ไม่ใช่คำกล่าวของ model
7. cleanup ถูกตรวจและบันทึก
8. final answer แยก verified, failed และ unverified claims
9. token/tool/time cost อยู่ใน budget
10. audit replay อธิบายได้ว่าทำไม runtime อนุญาตแต่ละ action

หลักการสำคัญที่สุดคือ:

> ให้ LLM เสนอความหมายและทางเลือก แต่ให้ runtime เป็นผู้ตัดสิน policy, state,
> retry, evidence และ completion

แนวทางนี้ทำให้โมเดลระดับล่าง–กลางใช้งานได้ดีขึ้นโดยไม่ต้องหวังว่า prompt จะควบคุม
พฤติกรรมได้ทุกครั้ง และยังลดค่าใช้จ่ายเพราะจำนวน repair/retry/verification ที่ไม่จำเป็นลดลง

