# แผนปรับปรุง UI/UX ของ Marcus Code

> เอกสารนี้สรุปจุดแข็ง จุดอ่อน และแผนการพัฒนาประสบการณ์ผู้ใช้ของ Marcus CLI โดยเน้นสิ่งที่ทำได้จริงและมีผลกระทบสูง

---

## 1. สถานะปัจจุบัน (Current State)

### 1.1 สิ่งที่มีแล้ว

| ส่วน | รายละเอียด |
|---|---|
| **TerminalUI** | สร้างด้วย `rich.console.Console` + `prompt_toolkit.PromptSession` |
| **Banner** | แสดง ASCII logo, workspace, model, provider, mode, profile, version |
| **Slash commands** | `/help`, `/model`, `/usage`, `/steps`, `/status`, `/compact`, `/retry`, `/continue`, `/clear`, `/mode`, `/config`, `/exit`, `/quit` |
| **Live step panel** | แสดง tool call/result/error แบบ real-time ด้วย `rich.live.Live` |
| **Bottom toolbar** | เวลาเซสชัน, context usage bar, model, token, throughput, mode |
| **Approval prompt** | y/n/a สำหรับ risky tool (`edit_file`, `run_cli`, `start_process`) |
| **Usage panel** | สถิติ token, call, budget, throughput |
| **Thinking indicator** | `think...` ขณะ LLM คิด → `thought: X.XXs` เมื่อเสร็จ |
| **Todo tracker** | มี `TodoTracker` + `Phase` ใน `marcus_code/todo_tracker.py` |

### 1.2 ข้อจำกัดที่พบ

- **`update_todo()` ยังไม่มีใน `TerminalUI`** แม้ `MarcusLoop` จะเรียกผ่าน `hasattr` ทำให้ผู้ใช้ไม่เห็น workflow phase ที่ agent กำลังทำ
- **การสตรีมข้อความจาก LLM** (`print_assistant_delta`) เป็น plain text ธรรมดา ไม่ render Markdown และอาจทับซ้อนกับ live panel
- **Auto-suggest ของ slash command** แสดงแค่ชื่อคำสั่ง ไม่มีคำอธิบาย
- **`/help` เป็น plain text** อ่านยากและไม่สวย
- **Approval UX** แจ้งเตือน dangerous command ได้ไม่ครอบคลุม และ clear prompt ด้วย ANSI math ที่เปราะบาง
- **ไม่มี no-color / high-contrast mode**
- **ไม่มี history ถาวร** (`InMemoryHistory`) และไม่รองรับ multi-line input
- **Guardrail messages** เป็นแค่บรรทัดสีแดง ไม่มี recovery hint
- **ไม่มีวิธีบันทึกผลลัพธ์** ของเทิร์น (session artifact)

---

## 2. เป้าหมาย (Goals)

1. ผู้ใช้เห็นว่า agent กำลังอยู่ใน phase ไหนของ workflow ตลอดเวลา
2. ข้อความจาก LLM render สวยและไม่ทับกับ live panel
3. ค้นพบ slash command ได้ง่ายขึ้น
4. Approval สำหรับ risky tool ชัดเจนและปลอดภัยขึ้น
5. รองรับ terminal ที่ไม่มีสี/Unicode
6. ลดความลำบากในการพิมพ์คำสั่งซ้ำและข้อความยาว
7. บันทึกผลลัพธ์ของเทิร์นได้เมื่อต้องการ

---

## 3. แผนการพัฒนา (Roadmap)

### Phase 1 — แก้จุดบอดที่ทำให้เสีย UX ทันที (HIGH impact)

#### 3.1 แสดง Todo/Workflow Progress

**ไฟล์:** `marcus_code/ui.py`, `marcus_code/loop.py`

**งาน:**
- เพิ่ม `TerminalUI.update_todo(todo: TodoTracker)`
- Render pipeline 6 ขั้นตอน:
  - `รับคำสั่ง → วิเคราะห์ → วางแผน → ดำเนินการ → ตรวจสอบ → ส่งมอบ`
- เน้นขั้นตอนปัจจุบัน (bold + สี) ขั้นตอนที่เสร็จแล้ว (dim + ✓) ขั้นตอนที่ยังไม่ถึง (dim)
- แสดงใน live panel (`_working_renderable`) และ/หรือ bottom toolbar
- เก็บ phase สั้น ๆ ไว้ใน `_bottom_toolbar` เช่น `Phase: วางแผน`

**Success criteria:**
- `tests/test_marcus_code_ui.py` มี test ตรวจ `update_todo` ทุก phase
- `tests/test_marcus_code_loop.py` ตรวจว่า loop เรียก `update_todo` ตาม phase

---

#### 3.2 ปรับปรุง Thinking Indicator

**ไฟล์:** `marcus_code/ui.py`, `marcus_code/loop.py`

**งาน:**
- ย้าย thinking indicator เข้าไปอยู่ใน live panel (spinner + elapsed time)
- เมื่อ LLM เสร็จ เปลี่ยนเป็น `thought: X.XXs` ใน status/step area
- รองรับกรณี timeout / error โดยไม่ทิ้ง `think...` ค้าง

**Success criteria:**
- ไม่มี `think...` ค้างบนหน้าจอเมื่อเกิด error
- test `test_thinking_indicator_*` ยังผ่าน

---

#### 3.3 ปรับ Streaming Output ให้ใช้งานได้

**ไฟล์:** `marcus_code/ui.py`, `marcus_code/loop.py`

**งาน (เลือกวิธีที่ง่ายก่อน):**

**วิธี A — Batched streaming (แนะนำ):**
- สะสม delta ไว้ในบัฟเฟอร์
- หยุด live panel ชั่วคราว
- เมื่อ streaming เสร็จ เรียก `print_assistant` เหมือนเดิม
- แสดงสถานะ "streaming..." ใน live panel ขณะรอ

**วิธี B — Real-time streaming region:**
- สร้าง `Live` region สำหรับ assistant output แยก
- อัปเดต Markdown ทีละ chunk
- ซับซ้อนกว่าและเสี่ยงกระพริบ

**Success criteria:**
- ข้อความ assistant ไม่ทับกับ live step panel
- test ใหม่ `test_streaming_output_does_not_corrupt_live_panel`

---

### Phase 2 — Command Discoverability & Help

#### 3.4 Rich `/help` Panel

**ไฟล์:** `marcus_code/ui.py`

**งาน:**
- เปลี่ยน `/help` จาก plain text เป็น `rich.table.Table` หรือ `rich.panel.Panel`
- คอลัมน์: Command, Arguments, Description
- จัดกลุ่มคำสั่งตามหมวดหมู่ (Session, Context, Info, Config, Exit)

**Success criteria:**
- `test_print_help_uses_table_or_panel` ผ่าน
- อ่านง่ายกว่าเดิม

---

#### 3.5 Auto-suggest ที่มีคำอธิบาย

**ไฟล์:** `marcus_code/ui.py`

**งาน:**
- ขยาย `SlashCommandAutoSuggest` ให้แสดงคำอธิบายสั้น ๆ ของคำสั่งที่ match
- เช่น `/clear  — Clear conversation context`
- รองรับ fuzzy matching เบื้องต้น (`/hepl` → `/help`)

**Success criteria:**
- `test_auto_suggest_shows_description` ผ่าน
- `test_auto_suggest_corrects_typos` ผ่าน

---

### Phase 3 — Approval Safety & Tool UX

#### 3.6 ปรับปรุง Approval UX

**ไฟล์:** `marcus_code/ui.py`, `marcus_code/tools.py` หรือ policy module ใหม่

**งาน:**
- ขยาย dangerous command patterns (เช่น `dd`, `mkfs`, `chmod -R /`, `> /dev/`, `rm -rf /`, `curl ... | sh`)
- แสดง risk level ใน approval panel (`low/medium/high`)
- ใช้ `rich.panel.Panel` แสดง tool name, arguments, risk, impact
- เปลี่ยน prompt ให้ชัดเจน: `(y)es / (n)o / (a)lways`
- แก้ `_clear_approval_prompt` ให้ robust ขึ้นด้วย prompt_toolkit หรือ `console.input` ที่ไม่ต้อง erase เอง

**Success criteria:**
- `test_command_warning_detects_dd_mkfs` ผ่าน
- `test_approval_panel_shows_risk` ผ่าน
- ไม่มี ANSI garbage เมื่อ terminal ไม่ใช่ TTY

---

### Phase 4 — Accessibility & Preferences

#### 3.7 No-color / High-contrast mode

**ไฟล์:** `marcus_code/ui.py`, `marcus_code/cli.py`

**งาน:**
- เพิ่ม `Theme` dataclass (`info`, `error`, `success`, `warning`, `accent`, `muted`)
- ตรวจ `NO_COLOR` env var และ `console.is_terminal`
- เพิ่ม CLI flag `--no-color` และ slash command `/theme [dark/light/high-contrast/no-color]`
- แทนที่ Unicode block ใน toolbar ด้วย ASCII (`#`/`-`) เมื่อไม่มี Unicode

**Success criteria:**
- `test_no_color_mode_uses_plain_text` ผ่าน
- `test_theme_command_switches_palette` ผ่าน

---

#### 3.8 Persistent History + Multi-line Input

**ไฟล์:** `marcus_code/ui.py`, `marcus_code/config.py`

**งาน:**
- เปลี่ยน `InMemoryHistory` เป็น `FileHistory` ที่ `~/.marcus/history`
- เพิ่ม keybinding `Shift+Enter` สำหรับ multi-line input (หรือ `/edit` command ที่เปิด editor ชั่วคราว)
- เพิ่ม `Ctrl+R` สำหรับ history search

**Success criteria:**
- ประวัติคำสั่งไม่หายหลัง restart
- สามารถส่ง prompt หลายบรรทัดได้

---

### Phase 5 — Polish & Utility

#### 3.9 ปรับปรุง Guardrail Messages

**ไฟล์:** `marcus_code/ui.py`, `marcus_code/loop.py`

**งาน:**
- ห่อ guardrail ด้วย `Panel` พร้อมไอคอน/สีและ recovery hint
- เก็บ last guardrail reason ใน UI สำหรับ slash command `/last`
- แยก recoverable vs fatal guardrails ด้วยสี/ข้อความ

**Success criteria:**
- `test_guardrail_panel_shows_recovery_hint` ผ่าน
- `/last` แสดงเหตุผลล่าสุด

---

#### 3.10 Save Turn Output

**ไฟล์:** `marcus_code/commands.py`, `marcus_code/ui.py`

**งาน:**
- เพิ่ม slash command `/save [path]`
- เขียนไฟล์ Markdown ประกอบด้วย:
  - user prompt
  - สรุป steps (จาก `_last_step_lines`)
  - final answer
  - token usage
  - guardrail reason (ถ้ามี)

**Success criteria:**
- `test_save_command_writes_markdown` ผ่าน
- ไฟล์ถูกเขียนที่ default path `~/.marcus/sessions/YYYY-MM-DD_HH-MM-SS.md` เมื่อไม่ระบุ path

---

#### 3.11 Responsive Layout

**ไฟล์:** `marcus_code/ui.py`

**งาน:**
- ลด manual truncation ใน `_steps_renderable` แล้วให้ Rich wrap เอง
- กำหนด max-width ให้ tool argument ที่ยาว
- ซ่อน/ย่อ workspace path เมื่อ terminal แคบ
- ใช้ `Group` layout ที่ปรับตามความสูงหน้าจอ

**Success criteria:**
- แสดงผลไม่พังบน terminal 80x24
- ไม่มี ANSI overflow บน narrow terminal

---

## 4. ลำดับความสำคัญ (Priority Matrix)

| ลำดับ | ฟีเจอร์ | Impact | Effort | ไฟล์หลัก |
|---|---|---|---|---|
| 1 | Todo/workflow display | HIGH | SMALL | `ui.py`, `loop.py` |
| 2 | Thinking indicator polish | HIGH | SMALL | `ui.py`, `loop.py` |
| 3 | Batched streaming output | HIGH | MEDIUM | `ui.py`, `loop.py` |
| 4 | Rich `/help` + auto-suggest | MEDIUM-HIGH | SMALL | `ui.py` |
| 5 | Approval UX + safety | MEDIUM-HIGH | MEDIUM | `ui.py`, policy |
| 6 | Persistent history + multi-line | MEDIUM-HIGH | MEDIUM | `ui.py`, `config.py` |
| 7 | No-color / theme | MEDIUM | MEDIUM | `ui.py`, `cli.py` |
| 8 | Guardrail panels + `/last` | MEDIUM | SMALL | `ui.py`, `loop.py` |
| 9 | Responsive layout | MEDIUM | MEDIUM | `ui.py` |
| 10 | `/save` turn output | LOWER | SMALL | `commands.py`, `ui.py` |

---

## 5. แนวทางการ Implement

### 5.1 หลักการ

- **Minimal changes:** แก้ไขเฉพาะ UI layer ให้มากที่สุด ไม่เปลี่ยน loop logic ที่มีอยู่
- **Backward compatible:** ใช้ `hasattr` ต่อไป แต่ตอนนี้ method ต้องมีจริง
- **Test-driven:** เขียน test ก่อนหรือพร้อมกับ implementation
- **No external deps ที่ไม่จำเป็น:** ใช้ Rich + prompt_toolkit ที่มีอยู่

### 5.2 ขั้นตอนที่แนะนำ

1. **สร้าง branch:** `ui-improvements`
2. **Phase 1:** implement todo display + thinking indicator + batched streaming
3. **Phase 2:** rich help + auto-suggest
4. **Phase 3:** approval safety
5. **Phase 4:** theme + history
6. **Phase 5:** guardrail panels + `/save` + responsive layout
7. **รัน test suite:** `uv run pytest tests/test_marcus_code_ui.py tests/test_marcus_code_loop.py tests/test_marcus_code_commands.py`
8. **Reinstall tool:** `uv tool install --force --reinstall .`

---

## 6. เกณฑ์ความสำเร็จ (Definition of Done)

- [ ] ผู้ใช้เห็น workflow phase ขณะ agent ทำงาน
- [ ] `think...` ไม่ค้างและ `thought: X.XXs` แสดงถูกต้อง
- [ ] ข้อความ assistant ไม่ทับกับ live panel
- [ ] `/help` อ่านง่ายและ auto-suggest มีคำอธิบาย
- [ ] Approval สำหรับ dangerous command ชัดเจน
- [ ] รองรับ `--no-color` และ theme
- [ ] ประวัติคำสั่งถาวร + multi-line input ทำงานได้
- [ ] Guardrail แสดง recovery hint และ `/last` ใช้ได้
- [ ] `/save` เขียน Markdown ได้
- [ ] UI ไม่พังบน terminal 80x24

---

## 7. หมายเหตุ

- เอกสารนี้เขียนจากการวิเคราะห์ไฟล์ `marcus_code/ui.py`, `marcus_code/loop.py`, `marcus_code/commands.py` และ test files ในวันที่ 18 July 2026
- เวอร์ชันปัจจุบันของ Marcus Code คือ `1.0.0.1`
- ควร merge แต่ละ phase แยกกัน เพื่อให้ review ง่ายและ revert ได้
