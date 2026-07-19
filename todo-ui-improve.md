# todo-ui-improve

เป้าหมาย: ปรับ Marcus CLI ให้เข้ากับแนวทาง **Typer + Rich + prompt_toolkit** (Phase 1)
โดยลดกล่อง (`rich.Panel`), เลิกใช้ live-refresh panel, และหันมาใช้
**append-only event timeline** เพื่อให้ terminal scrollback/copy ใช้งานง่ายและ
พฤติกรรมใกล้ Claude Code / Codex CLI

---

## A — UI simplification (ทำก่อน)

Scope: แก้เฉพาะ `marcus_code/ui.py` เป็นหลัก ไม่ย้ายไฟล์/เปลี่ยน public API
ของ `TerminalUI` ที่ `MarcusLoop`, `cli.py`, `commands.py` ใช้อยู่

### A1. ลบ Panel / Live ออกจาก chat loop
- [ ] ลบ `rich.Live` และ `_working_renderable()` panel — เปลี่ยนเป็น
      append-only lines ที่พิมพ์ครั้งเดียวเมื่อ event เกิด
- [ ] `print_tool_call` → พิมพ์ `● {action}` + `  ├─ {tool}({args})` ลง console
      ตรง ๆ ไม่ผ่าน buffer
- [ ] `print_tool_result` → `  └─ ✓ {summary}` inline
- [ ] `print_tool_error` / `print_tool_declined` → `  └─ × {msg}` inline
- [ ] `start_thinking` / `stop_thinking` → ใช้ `rich.status.Status` แบบ transient
      หรือแค่ inline `⋯ thinking...` แล้วเขียนทับด้วย carriage return
- [ ] `finish_steps` → เหลือแค่ 1 บรรทัด `✓ done · N tool(s)` (ไม่มี hint /steps
      เพราะเห็นเต็มอยู่แล้วใน scrollback)
- [ ] `print_steps` / `_steps_renderable` → **ลบทิ้ง** (ข้อมูลอยู่ใน scrollback
      แล้ว) พร้อมลบ `/steps` command และ state `_last_step_lines`,
      `_step_lines`, `_working_lines`, `_steps_collapsed`

### A2. ลบกรอบออกจาก informational surfaces
- [ ] `print_help` → หัวเรื่อง + rule + table (ไม่มี Panel wrap)
- [ ] `print_config` → key/value inline + rule ปิดท้าย
- [ ] `print_usage` → key/value inline + rule ปิดท้าย
- [ ] `print_ollama_cloud_usage` → 2 บรรทัดตรง ๆ ไม่มี panel
- [ ] `run_first_time_setup` → ข้อความ intro แบบ inline
- [ ] `run_config_edit` → เช่นเดียวกัน
- [ ] `print_last_guardrail` → 1 บรรทัด `× last guardrail · {reason}`
- [ ] `print_guardrail_stop` → คงรูปแบบเดิม (inline อยู่แล้ว)

### A3. Banner — คงกรอบไว้
- ผู้ใช้ยืนยันว่า Panel รอบ banner ยังเก็บ (one-shot ตอน start ไม่ชน scrollback)
- ไม่แตะ `print_banner`

### A4. Approval prompt — ไม่แตะ
- ยังใช้ `console.input()` เดิม (InquirerPy อยู่ใน Phase 2 ตามที่ user ระบุ)
- Header inline (risk pill + tool name + args) คงรูปแบบเดิม

### A5. Bottom toolbar (persistent status header)
- คงไว้ตามเดิม — เป็น persistent status ที่ user ต้องการอยู่แล้ว
- ไม่แตะ `_bottom_toolbar()`

### A6. Cleanup
- [ ] ลบ import ที่ไม่ใช้ (`Panel`, `Live`, `Padding` ถ้าไม่จำเป็น)
- [ ] ลบ `refresh_status`, `_refresh_steps`, `_pause_live`, `_resume_live`,
      `_stop_live` ที่จะไม่มีการเรียกอีก (ต้องเช็ค `commands.py` และ `loop.py` ก่อน)
- [ ] update `/steps` ใน `command_info.py` / `commands.py` (ลบทิ้ง)

### A7. Verify
- [ ] `uv run marcus --help` ยังใช้ได้
- [ ] Smoke test: รัน 1 turn ที่มี tool call แล้วดู timeline ใน terminal
- [ ] ทดสอบ `--no-color` mode
- [ ] ตรวจว่าไม่มี Live overlap ตอนถามอนุมัติ tool

---

## B — Structural refactor

### B1. Typer แทน argparse  ✅ done
- `cli.py` ใช้ `typer.Typer` แล้ว: default callback รัน REPL, subcommand
  `update [version]` และ `version`; รักษา flags `-p/--prompt`, `--mode`,
  `--no-color`, `-v/--version`, `--update`, `-y/--yes`
- Test `test_main_rejects_unknown_subcommand` อัพเดตให้ยอมรับ error
  format ของ Typer

### B4. InquirerPy สำหรับ approvals  ✅ done
- `confirm_tool_call` เรียก `_approval_menu()` (InquirerPy `inquirer.select`)
  เมื่อ stdin/stdout เป็น TTY จริง; ตกกลับไปที่ y/n/a text prompt
  สำหรับ non-TTY (tests, piped stdin)
- Choices: `Apply once` / `Always allow this tool this session` / `Reject`

### B3. Folder restructure  ✅ done
- โครงจริงที่ใช้ (แตกต่างจาก plan เดิมเล็กน้อย — ไม่สร้าง shell.py /
  keybindings.py / approvals.py ที่ยังเป็น aspirational):
  ```
  marcus_code/
  ├── cli/         app.py, commands.py
  ├── ui/          console.py, banner.py, command_info.py, warnings.py, renderer.py
  ├── runtime/     agent.py (จาก loop.py), events.py, event_bus.py,
  │                modes.py, prompt.py, skills.py, task_contract.py,
  │                todo_tracker.py, token_utils.py, ollama_usage.py
  ├── tools/       base.py (จาก tools.py), extra.py (จาก tools_extra.py)
  └── state/       config.py
  ```
- ใช้ `git mv` ทุกไฟล์ → git track ได้ว่าเป็น rename
- Entry point: `marcus = "marcus_code.cli.app:main"`
- Bulk-rewrite import paths ทั่ว codebase + tests + monkeypatch strings
- fix: `_detect_install_method()` marker path ลึกขึ้นอีก 1 ระดับ
  (`cli/app.py → cli/ → marcus_code/ → repo root`)

### B2. Event bus + Pydantic events  ✅ done
- `runtime/events.py`: Pydantic BaseModel `_Event` + 17 event subclasses
  (`TurnStarted`, `TurnFinished`, `TodoUpdated`, `ThinkingStarted/Stopped`,
  `StreamStarted/Delta/Ended`, `AssistantMessage`, `FinalAnswer`,
  `ToolCallStarted/Completed/Failed/Declined`, `Recovery`, `GuardrailStop`,
  `Interrupted`); tagged union via `kind: Literal[...]`
- `runtime/event_bus.py`: synchronous fan-out `EventBus` — subscriber
  exceptions ถูก swallow เพื่อไม่ให้ renderer พังไปทำ agent ค้าง
- `ui/renderer.py`: `TerminalRenderer` แปลง Event → TerminalUI method call
- `runtime/agent.py`: constructor รับ `events: EventBus | None`; auto-wire
  `TerminalRenderer(ui)` เมื่อไม่ได้ส่ง bus มา (รักษา backwards-compat
  กับ MarcusLoop tests ที่ใช้ `_TestUI` mock อยู่แล้ว)
- 53 `self.ui.*` one-way call sites ถูกแทนด้วย `self.events.emit(...)`;
  เก็บ `self.ui.confirm_tool_call` และ `self.ui.refresh_status` ไว้ตรง ๆ
  เพราะเป็น two-way / no-op
- `cli/app.py` wire bus + renderer อย่างชัดเจนที่ boundary
- `tests/test_marcus_code_event_bus.py` ใหม่ 4 test — fan-out order,
  subscriber exception isolation, renderer dispatch table,
  end-to-end wiring

### B5. Textual dashboard (Phase 3)  ✅ done
- `pyproject.toml`: เพิ่ม optional extra `tui = ["textual>=0.85.0"]`
- `marcus_code/ui/tui.py`:
  - `ApprovalScreen` — Textual `ModalScreen[str]` พร้อม button + key
    binding (`y` / `a` / `n`+`esc`) สำหรับตอบ approval
  - `TuiPrompter` — adapter object แทน `TerminalUI` ที่ส่งเข้า MarcusLoop;
    exposes async `confirm_tool_call` (await modal), `refresh_status`
    (trigger app repaint) และ no-op stubs สำหรับ hasattr gate ทั้งหมด
    (ยกเว้น `start_stream`/`stream_delta`/`end_stream` — ปล่อยไว้เพื่อ
    ให้ loop เลือก non-streaming path)
  - `TuiRenderer` — subscribe `EventBus`, dispatch 15 event kinds เข้า
    `RichLog` (มี tool counter + phase breadcrumb dedupe เหมือน
    TerminalRenderer)
  - `MarcusTuiApp` — `App` ที่ประกอบ Static status header, RichLog
    (append-only timeline) และ Input; on submit → `asyncio.create_task`
    รัน turn/slash command; `Ctrl+C`/`Ctrl+D` ออก
- `runtime/agent.py`: `confirm_tool_call` ตรวจ `inspect.isawaitable`
  ผลลัพธ์ก่อน — ทำให้ prompter แบบ async (Textual) ใช้ได้ทันทีโดยไม่
  แตะ TerminalUI
- `cli/app.py`: subcommand `marcus tui` → `_amain_tui()` — wire
  `EventBus`, `TuiPrompter`, `MarcusTuiApp`; ใช้ `_AppProxy` แก้ chicken-
  and-egg (prompter ต้องอ้างถึง app ก่อน app ถูกสร้างเสร็จ); ถ้า
  Textual ไม่ได้ติดตั้งจะแนะนำ `marcus[tui]`
- `tests/test_marcus_code_tui.py`: 6 test ครอบ tool lifecycle, phase
  breadcrumb dedupe, fail/decline path, assistant/recovery/final,
  prompter stubs, และ `push_screen_wait` bridge — ใช้ `pytest.importorskip`
  ให้ suite ยัง green ถ้าไม่มี `tui` extra
