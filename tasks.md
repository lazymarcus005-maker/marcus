# Implementation Tasks

แตกจาก `review-open-questions.md` §4 ตามการตัดสินใจใน `decisions.md` — ทุก task เป็น GitHub issue แล้ว (เลข issue ตรงกับลำดับ)

## Phase 0 — Foundation
| Issue | Task |
|---|---|
| [#1](https://github.com/lazymarcus005-maker/marcus/issues/1) | Project scaffold: Python + FastAPI + uv + ruff + pytest + CI |
| [#2](https://github.com/lazymarcus005-maker/marcus/issues/2) | Docker Compose: PG + Redis + RabbitMQ + api + worker |
| [#3](https://github.com/lazymarcus005-maker/marcus/issues/3) | Core DB schema v1 + Alembic migrations |

## Phase 1 — Core Loop (ต้องเสถียรก่อนอย่างอื่น)
| Issue | Task |
|---|---|
| [#4](https://github.com/lazymarcus005-maker/marcus/issues/4) | Run state machine + repository layer (optimistic locking) |
| [#5](https://github.com/lazymarcus005-maker/marcus/issues/5) | LLM Gateway: OpenAI-compatible client (Ollama cloud) |
| [#6](https://github.com/lazymarcus005-maker/marcus/issues/6) | Custom ReAct loop engine |
| [#7](https://github.com/lazymarcus005-maker/marcus/issues/7) | finish tool + completion handling |
| [#8](https://github.com/lazymarcus005-maker/marcus/issues/8) | Guardrails v1 |
| [#9](https://github.com/lazymarcus005-maker/marcus/issues/9) | Tool execution write-ahead + idempotency (G2) |
| [#10](https://github.com/lazymarcus005-maker/marcus/issues/10) | Run lease + heartbeat + stale-run reaper (G1) |
| [#11](https://github.com/lazymarcus005-maker/marcus/issues/11) | Worker: RabbitMQ consumer |
| [#12](https://github.com/lazymarcus005-maker/marcus/issues/12) | Agent API v1 (create/message/poll/cancel) |
| [#13](https://github.com/lazymarcus005-maker/marcus/issues/13) | Context compaction: rolling summary (G5) |

## Phase 2 — Tools via MCP (HTTP only)
| Issue | Task |
|---|---|
| [#14](https://github.com/lazymarcus005-maker/marcus/issues/14) | MCP server registry + HTTP MCP adapter |
| [#15](https://github.com/lazymarcus005-maker/marcus/issues/15) | Progressive tool disclosure |
| [#16](https://github.com/lazymarcus005-maker/marcus/issues/16) | Tool result pipeline (normalize → truncate → persist) |
| [#17](https://github.com/lazymarcus005-maker/marcus/issues/17) | Permission tiers + approval flow |

## Phase 3 — Skills (manual lifecycle, self-learning เลื่อน)
| Issue | Task |
|---|---|
| [#18](https://github.com/lazymarcus005-maker/marcus/issues/18) | Skill registry: immutable revisions + publish/rollback |
| [#19](https://github.com/lazymarcus005-maker/marcus/issues/19) | Skill selection & injection (use_skill meta-tool) |
| [#20](https://github.com/lazymarcus005-maker/marcus/issues/20) | Skill usage + outcome logging (เตรียม self-learning) |

## Phase 4 — Channels & UI (Slack + Web, ไม่มี streaming)
| Issue | Task |
|---|---|
| [#21](https://github.com/lazymarcus005-maker/marcus/issues/21) | Slack integration: Events webhook + thread mapping |
| [#22](https://github.com/lazymarcus005-maker/marcus/issues/22) | Web UI: runs, conversation, approvals |
| [#23](https://github.com/lazymarcus005-maker/marcus/issues/23) | AuthN/AuthZ: API keys ต่อ tenant |

## Phase 5 — Ops
| Issue | Task |
|---|---|
| [#24](https://github.com/lazymarcus005-maker/marcus/issues/24) | Observability: OTel tracing + metrics |
| [#25](https://github.com/lazymarcus005-maker/marcus/issues/25) | Scheduler: APScheduler + scheduled_jobs |
| [#26](https://github.com/lazymarcus005-maker/marcus/issues/26) | Ops hardening: reapers, quotas, runbook |

## ลำดับ dependency หลัก

```text
#1 → #2 → #3 → #4/#5 → #6 → #7/#8/#9/#13
                #6 → #11 → #10, #12
#6 → #14 → #15/#16, (#9,#14) → #17
#3 → #18 → #19 (ต้อง #15) → #20
#12 → #21 → #23, (#12,#17) → #22
(#11,#12) → #24, (#11,#21) → #25, (#17,#24) → #26
```

Milestone แนะนำ:
* **M1 (Phase 0–1)**: คุยกับ agent ผ่าน API ได้ครบ loop, crash-safe
* **M2 (Phase 2)**: ใช้ tool จริงผ่าน MCP + approval
* **M3 (Phase 3–4)**: skills + Slack + Web UI = ใช้งานจริงได้
* **M4 (Phase 5)**: observability + scheduled runs
