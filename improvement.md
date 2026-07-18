# Improvement.md — Marcus Code (CLI Agent)

เอกสารนี้เจาะจง `marcus_code/` เท่านั้น — ส่วน CLI interactive coding agent
ที่รันอยู่ในเทอร์มินัลผู้ใช้และพึ่งพา `LLMGateway`, 12 built-in tools, และ
`MarcusLoop` ในหน่วยความจำ (ไม่มี DB run state)

ปัญหาสำคัญที่กำลังเกิดเมื่อสั่งทำงานกับ **repository จริง**คือ agent หยุดทำงานกลางคัน
หรือจบเทิร์นก่อนได้ผล สาเหตุเป็นได้หลายทาง แต่ละทางมี root cause และแนวแก้ต่างกัน
และบางทางทับซ้อนกับ roadmap ที่เคยลงไว้ใน `improvement.md` (งานฝั่ง server)

---

## 1. อาการที่เกิดจริงเมื่อสั่งกับ repository จริง

เมื่อรันเช่น `uv run marcus -p "fix the failing tests"` หรือพิมพ์งานใน
REPL โดยอยู่ในโฟลเดอร์ที่มีไฟล์จริงหลายร้อย/หลายพันไฟล์ เกิด:

1. **Agent จบเทิร์นทันที โดยทำ step เดียวหรือไม่ทำอะไรเลย** — เห็นเพียง
   คำตอบสั้นและ prompt กลับมาเสมือนเสร็จแล้ว
2. **ติดอยู่กับ guardrail แบบ "no progress" หรือ "repeated call"** หลังจาก
   tool แรกหรือ tool ที่สองทำงานสำเร็จแล้ว ทั้งที่ยังไม่ได้แตะส่วนที่เป็นปัญหา
3. **LLM call ค้างนานจนถึง `cli_llm_recovery_timeout_seconds = 90s`**
   แล้วแสดง "LLM recovery timed out" โดยไม่ได้เปิดโอกาส recover
4. **context บวมเร็วเกินจังหวะ** หลังจาก `list_files`, `grep`, หรืออ่านไฟล์ใหญ่
   จนต้อง compact แล้วบางครั้ง summary ทิ้ง fact ที่จำเป็นทำให้ step ถัดไป
   หลุดจากเป้าหมายเดิม
5. **finalization gate บล็อก final answer ครั้งเดียวแล้วจบเทิร์น** เพราะ
   `finalization_repairs < 1` อนุญาต repair เพียงครั้งเดียวแล้ว return

อาการเหล่านี้ไม่ได้เกิดกับโค้ดเล็กที่เขียนเองใน sandbox แต่เกิดกับ repository จริงที่
มี noise directory มาก, ไฟล์ใหญ่, คำสั่งช้า, และเนื้อหาที่ทำให้ context บวมเร็ว

---

## 2. Root cause ที่พบในโค้ดปัจจุบัน

### 2.1 การ์ด finalization อนุญาต repair ครั้งเดียวเกินไปเข้มงวด

`marcus_code/loop.py:208-225` — ถ้า task ต้อง verification ตาม
`TaskContract.requires_verification` (เกิดจาก keyword เช่น `test`, `build`,
`pytest`, `curl`, `verify`) แล้วเมื่อโมเดลตอบเป็น plain text โดยยังไม่มี
evidence ระบบแทรก system message สั่งให้รัน verification หนึ่งครั้งแล้วให้
สรุปผล แต่ `finalization_repairs < 1` หมายความ repair ได้แค่ครั้งเดียวเท่านั้น
ถ้า verification call แรกยังไม่พอ (เช่นโมเดลเรียกผิด tool, arguments ผิด,
หรือต้อง build ก่อน test) ระบบก็หยุดด้วยข้อความ "final answer blocked:
requested verification has no successful evidence" และ return ทันที

ผู้ใช้เห็นเป็น "หยุดทำงานกลางคันทั้งที่ยังไม่เสร็จ"

### 2.2 `max_tool_calls_per_step = 1` รวมถึง read-only calls ที่ควร parallel ได้

`marcus_code/loop.py:246-266` — นโยบายปัจจุบันปฏิเสธทุก tool call ที่สองใน
LLM response เดียวกันด้วย `POLICY_DENIED` แล้วส่ง observation error ให้
โมเดล บังคับให้รอ step ใหม่เสมอ

กับ repository จริง โมเดลมักเสนอ `list_files` + `read_file` + `grep` ใน
response เดียวเพื่อสำรวจโค้ด ระบบปฏิเสธสองจากสาม ทำให้เสีย step ไปหลาย
รอบเพื่อแค่รวบรวมข้อมูลพื้นฐาน และบางครั้งโมเดลเลิกพยายามและตอบ plain text
ทำให้เทิร์นจบเร็วผิดจังหวะ

### 2.3 `grep` และ `list_files` ไม่กรองไฟล์ตามขนาดและประเภทอย่างพอใจ

`marcus_code/tools.py:238-295` — `_MAX_GREP_FILE_BYTES = 2_000_000`
และ `_SKIP_DIR_NAMES` กรอง `.git`, `node_modules`, `.venv` ฯลฯ แล้ว แต่
ไม่กรองไฟล์ binary ตาม content type, ไม่จำกัดจำนวนไฟล์ที่ scanned, และไม่
sort ผลลัพธ์ตาม relevance ใน repository ขนาดกลาง–ใหญ่ `grep` ที่เรียบง่าย
นี้สแกนไฟล์ทุกไฟล์ใน glob และคืน `matches` สูงสุด 200 รายการแรก ทำให้
LLM ได้ผลลัพธ์เป็น noise มากกว่าสัญญาณและบางครั้ง context บวมทันที

`list_files` ใน repo จริงส่งกลับ path แบบเรียบ 200 รายการแรกโดยไม่
บอกว่ามีโฟลเดอร์หลักอะไรบ้าง โมเดลจึงมักเรียกซ้ำเพื่อสำรวจต่อ

### 2.4 `read_file` ไม่มี chunking หรือ offset/limit ทำให้ไฟล์ใหญ่เป็น context bomb

`marcus_code/tools.py:77-107` — อ่านไฟล์ทั้งไฟล์ทันทีแล้วส่ง content ทั้งหมด
เข้า LLM ในไฟล์ 5,000+ บรรทัด content เดียวก็กิน context window 20–40k
tokens ทำให้ `context_percent` กระโดดขึ้น ครั้งเดียวก็เข้า compact และ
อาจเข้า compact ซ้ำจน history สั้นลงเกินไปและโมเดลหลุดจากเป้าหมาย

### 2.5 ไม่มี progressive disclosure ตามขนาดงาน ทำให้โมเดลเล็กหลงทาง

`marcus_code/prompt.py` — system prompt เดียวกันใช้กับทุกงาน: เล็ก
เช่น "อ่านไฟล์นี้แล้วสรุป" หรือใหญ่เช่น "แก้บั๊กในระบบ" ไม่มีการเพิ่ม hint
ตาม `TaskKind` ที่ `task_contract.py` แยกไว้ โมเดลเล็กมักตีความงานใหญ่เป็น
"อ่าน 1–2 ไฟล์แล้วตอบ" แล้วจบเทิร์น

### 2.6 `run_cli` timeout 30 วินาทีเข้มงวดเกินไปสำหรับงานจริง

`harness/config.py:67` — `tools_run_cli_timeout_seconds = 30.0`
ใน repository จริงคำสั่งเช่น `pytest`, `npm test`, `dotnet build` อาจใช้
60–180 วินาที คำสั่งถูกฆ่าที่ 30s โดย return error แล้วโมเดลเข้าใจว่า
"build fail" ทั้งที่จริงยังไม่เสร็จ จบลงด้วย final answer ผิด

### 2.7 `compact_history` ทำซ้ำจน history สั้นเกินไป

`marcus_code/loop.py:368-382` — ลด `max_history_messages` ทีละหนึ่งใน
while loop จนกว่า `context_tokens` จะต่ำกว่า target ใน repository จริงที่
มี tool result ใหญ่ compact หนึ่งครั้งสามารถตัดประวัติลงเหลือ 4 ข้อความ
แล้วโมเดลหลุดจากบริบทที่จำเป็นต่อการทำงานต่อ

### 2.8 `_trim_history` ไม่ preserve evidence/decisions อย่างชัดเจน

`marcus_code/loop.py:334-356` — ตัดประวัติเก่าแล้วแทนด้วย summary snippet
เพียง 12 ข้อความสุดท้าย สูงสุด 240 ตัวอักษร/ข้อความ evidence สำคัญเช่น
"build passed at step 5", "test count = 42", "process_id = abc123"
อาจถูกตัดออก ทำให้โมเดลเสีย state และตอบผิดหรือหยุด

### 2.9 LLM recovery timeout ไม่มี fallback ที่เป็นประโยชน์

`marcus_code/loop.py:160-199` — `asyncio.timeout(90s)` ครอบ `complete`
หรือ `complete_stream`; เมื่อหมดเวลาก็แค่ return และจบเทิร์น ไม่มีการ
- ลอง complete (non-stream) เป็น fallback ของ stream timeout
- ลด context แล้ว retry ครั้งเดียว
- แจ้งผู้ใช้และถามว่าจะรอต่อหรือยกเลิก

### 2.10 ไม่มี resume/continue หลัง guardrail stop

เมื่อ guardrail หยุดเทิร์นด้วยเหตุผลใดก็ตาม `run_turn` เพียง return
และ REPL รอ prompt ใหม่ ไม่มีคำสั่ง `/continue` หรือ `/retry` ที่
อนุญาตให้กลับเข้า loop ด้วย state เดิมและข้าม guardrail ที่จบไปแล้วได้

---

## 3. แนวแก้เฉพาะ `marcus_code` (ไม่แตะ server harness)

### 3.1 ปล่อย finalization repair ได้มากขึ้นและ track evidence ที่ขาด

ใน `marcus_code/loop.py`:

```python
# ปัจจุบัน
if finalization_repairs < 1:

# แนวแก้
max_finalization_repairs = 3
if finalization_repairs < max_finalization_repairs:
    finalization_repairs += 1
    missing = contract.missing_evidence_hint(verification_succeeded)
    self.state.history.append(LLMMessage(..., content=(
        "Finalization denied: required verification not satisfied. "
        f"Specifically: {missing}. "
        "Run one appropriate verification tool, then summarize its result."
    )))
    continue
```

เพิ่ม `TaskContract.missing_evidence_hint()` ที่คืนข้อความเฉพาะเช่น
"no test command succeeded yet" หรือ "no HTTP check passed" เพื่อ
ชี้แนะโมเดลแบบ concrete แทนข้อความเดียวกันทุกครั้ง

### 3.2 ยอม read-only parallel calls แต่ยังบล็อก mutation ซ้อน

แยก policy ตาม risk tier แทน "one call เสมอ":

```python
read_only_calls = [c for c in response.tool_calls
                   if self.tools_by_name[c.name].risk_tier == RiskTier.read_only]
mutation_calls  = [c for c in response.tool_calls
                   if self.tools_by_name[c.name].risk_tier != RiskTier.read_only]

# read-only อนุญาตได้พร้อมกัน (bounded, เช่น ≤ 3)
# mutation ยังทีละรายการเท่านั้น
```

ลด step ที่เปล่าไปกรณีสำรวจ 3 ไฟล์พร้อมกัน, และยังกัน mutation ซ้อนที่
อาจพึ่ง output ของกันและกัน

### 3.3 ปรับ `grep` และ `list_files` ให้ใช้ได้กับ repo จริง

- `grep`: เพิ่ม parameter `max_results` (default 50), `file_pattern`
  เพื่อจำกัด, และเรียงผลตามความสำคัญ (file path match, line แรกก่อน)
- `grep`: skip ไฟล์ที่ไม่ใช่ text โดยดู BOM/NULL bytes แทนเพียง
  `UnicodeDecodeError` catch
- `list_files`: แยกผลเป็น directory summary + file list, และเพิ่ม
  parameter `max_depth` เพื่อจำกัดความลึก
- ทั้งสอง: ส่งกลับ `total_matches` หรือ `total_files` นอกเหนือจาก
  `truncated` เพื่อให้โมเดลรู้ว่ายังมีข้อมูลที่ไม่ได้แสดง

### 3.4 ให้ `read_file` รองรับ offset/limit และ streaming chunk

เพิ่ม parameters:

```json
{
  "path": "src/big.py",
  "offset": 1,
  "limit": 200
}
```

และเมื่อไฟล์ใหญ่เกิน `max_chars` ให้ส่งกลับบอกจำนวนบรรทัดทั้งหมดและ
แนะนำให้ใช้ offset ถัดไป แทนส่งทั้งไฟล์แล้ว compact รุนแรง

### 3.5 ขยาย `run_cli` timeout ตามประเภทคำสั่งและอนุญาต override

```python
# แทนค่าคงที่ 30s
def _cli_timeout_for(command: str) -> float:
    lowered = command.lower()
    if any(k in lowered for k in ("pytest", "npm test", "dotnet build", "cargo test")):
        return 180.0
    if any(k in lowered for k in ("build", "compile", "webpack")):
        return 120.0
    return settings.tools_run_cli_timeout_seconds
```

หรือเปิด parameter `timeout_seconds` ใน tool schema (capped ที่ 300)
แล้วให้โมเดลเลือกได้

### 3.6 compact แบบ preserve evidence และไม่ตัดรุนแรง

- ก่อน compact ให้แยก history เป็นสามชั้น:
  1. **protected**: system, user เดิม, evidence message (tool call +
     result ที่ผ่าน `is_verification_evidence`), และแผนที่โมเดลแสดง
  2. **keep**: ข้อความ 6 ล่าสุด
  3. **droppable**: ที่เหลือ
- compact เฉพาะ droppable, แล้วสร้าง summary ที่ preserve fact สำคัญ
  เช่น exit code, process_id, test count, จาก droppable messages
- ลด `max_history_messages` ทีละห้าไม่ใช่ทีละหนึ่ง เพื่อลด iteration
  และลดโอกาส trim รุนแรง
- เก็บ protected message ไว้ใน `SessionState.protected` แยกจาก
  `history` แล้ว inject ใหม่ทุก LLM call

### 3.7 recovery timeout ที่เป็นประโยชน์

```python
except TimeoutError:
    # แทน return ทันที ลด context แล้ว retry ครั้งเดียว
    if not retried_after_timeout:
        retried_after_timeout = True
        self.compact_history()
        self.ui.print_recovery("LLM timed out; retrying with compacted context.")
        continue
    self.ui.print_guardrail_stop(...)
    return
```

### 3.8 เพิ่มคำสั่ง `/continue` และ `/retry`

เก็บ last turn state ใน `SessionState`:

```python
@dataclass
class SessionState:
    history: list[LLMMessage] = field(default_factory=list)
    always_allowed: set[str] = field(default_factory=list)
    last_turn_input: str | None = None
    last_turn_guardrail: str | None = None
```

เมื่อ guardrail หยุดเทิร์น ให้ `ui.prompt_user()` แสดง hint:
"stopped: <reason>. ใช้ /retry เพื่อลองใหม่ หรือ /continue เพื่อข้าม"

`/retry` เรียก `run_turn(last_turn_input)` ใหม่
`/continue` เรียก `run_turn("continue from where you stopped")` โดย
inject system message บอก state ปัจจุบัน

### 3.9 task contract hint ตาม TaskKind

เพิ่มใน system prompt ที่ `build_system_prompt`:

```python
kind_hint = {
    TaskKind.explain: "อ่านให้ครบก่อนสรุป อย่าตอบก่อนได้ข้อมูลครบ",
    TaskKind.change:  "สำรวจ → แผน → เปลี่ยน → ตรวจสอบ ตามลำดับ",
    TaskKind.operate:  "start_process → wait → ตรวจสอบ → stop_process",
}
```

และ inject `kind` กับข้อกำหนดเข้า history เป็น system message แรกของ
เทิร์นเพื่อให้โมเดลเล็กเห็นกรอบชัด

### 3.10 context meter ที่แสดง budget จริง

เพิ่มใน `refresh_status` / `_bottom_toolbar`:

- แสดง `max_steps` และ `current_step` ที่กำลังรัน (loop.py ต้อง expose)
- แสดง `verification_succeeded` flag
- แสดง "finalization repair X/3" เมื่ออยู่ในขั้นนั้น

ช่วยให้ผู้ใช้เห็นว่า agent กำลังทำอะไรและทำไมยังไม่จบแทน "ดูเหมือนค้าง"

---

## 4. ลำดับการทำ

### Priority 1 — แก้ "หยุดทำงานกลางคัน" โดยตรง

1. **ขยาย finalization repair เป็น 3 และใส่ missing-evidence hint**
   (3.1) — แก้อาการ 1 และ 5 โดยตรง
2. **ยอม read-only parallel calls** (3.2) — ลด step เปล่าในการสำรวจ
3. **เพิ่ม `/continue` และ `/retry`** (3.8) — ให้ผู้ใช้กู้เทิร์นที่หยุดได้
4. **recovery timeout ที่ compact แล้ว retry ครั้งเดียว** (3.7) — แก้อาการ 3

### Priority 2 — ทำให้ใช้ได้กับ repository จริง

5. **`read_file` offset/limit** (3.4) — กัน context bomb จากไฟล์ใหญ่
6. **`grep` และ `list_files` ที่กรองและ sort ดีขึ้น** (3.3) — ลด noise
7. **`run_cli` timeout ตามประเภทคำสั่ง** (3.5) — กัน build/test ถูกฆ่าก่อนเสร็จ
8. **compact แบบ preserve evidence** (3.6) — กันหลุดเป้าหมายหลัง compact

### Priority 3 — คุณภาพและความชาญฉลาด

9. **task contract hint ใน system prompt** (3.9)
10. **context meter ที่แสดง state จริง** (3.10)

---

## 5. วิธียืนยันว่าแก้แล้ว

หลังลงมือ Priority 1 รันกับ repository จริงที่เคยหยุด:

- `uv run marcus -p "run pytest and summarize failures"` — ต้องไม่
  จบก่อน pytest เสร็จ และต้องมี evidence ใน final answer
- สั่ง "อ่านไฟล์ X, Y, Z แล้วสรุป" — ต้องอ่านครบทั้งสามใน step ใกล้เคียง
  ไม่ใช่ step ละไฟล์
- สั่งงานยาวแล้วกด Ctrl+C ตอด mid-step — `/retry` ต้องกลับเข้า loop ได้
  โดยไม่เสีย context สำคัญ

หลัง Priority 2:

- `grep` ใน repo 1,000 ไฟล์ต้องคืน < 1 วินาที และผลเป็นไฟล์ที่เกี่ยวข้อง
- อ่านไฟล์ 5,000 บรรทัดต้องไม่ทำให้ context บวมเกิน 15%
- `dotnet build` ที่ใช้ 90 วินาทีต้องไม่ถูกฆ่าที่ 30s

---

## 6. หมายเหตุ

- แนวแก้ทั้งหมดนี้อยู่ใน `marcus_code/` และ `harness/config.py` เท่านั้น
  ไม่กระทบ `harness/runtime/engine.py` หรือ server harness
- บางข้อ (เช่น evidence preservation, typed outcome) ทับซ้อนกับ
  `improvement.md` ของฝั่ง server แต่ใช้ implementation แยกเพราะ CLI
  เก็บ state ในหน่วยความจำไม่ใช่ DB
- task contract และ verification gate ของ CLI ปัจจุบันเป็น heuristic
  อิง keyword เท่านั้น หากต้องการให้แม่นยำขึ้นต้องเพิ่ม explicit `--verify`
  flag หรือให้ผู้ใช้ mark task ที่ต้อง verification แบบชัดแจ้ง