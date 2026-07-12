import re
from dataclasses import dataclass
from enum import StrEnum


class TaskKind(StrEnum):
    explain = "explain"
    change = "change"
    operate = "operate"


@dataclass(frozen=True)
class TaskContract:
    kind: TaskKind
    requires_plan: bool
    requires_verification: bool


_CHANGE = re.compile(
    r"(?i)\b(add|build|change|create|edit|fix|implement|install|refactor|remove|update|write)\b"
    r"|(?:เพิ่ม|แก้|สร้าง|ติดตั้ง|เขียน|ปรับปรุง|ดำเนินการ)"
)
_OPERATE = re.compile(
    r"(?i)\b(run|start|stop|deploy|serve|launch)\b|(?:รัน|เปิด|ปิด|ทดสอบระบบ)"
)
_VERIFY = re.compile(
    r"(?i)\b(build|curl|lint|test|verify|verification|pytest|dotnet\s+run|npm\s+test)\b"
    r"|(?:ทดสอบ|ยืนยันผล|ตรวจสอบผล|รัน service|รันเซอร์วิส)"
)


def derive_task_contract(user_input: str) -> TaskContract:
    """Conservatively infer only runtime-enforceable requirements from the request."""
    requires_verification = bool(_VERIFY.search(user_input))
    if _CHANGE.search(user_input):
        kind = TaskKind.change
    elif _OPERATE.search(user_input):
        kind = TaskKind.operate
    else:
        kind = TaskKind.explain
    return TaskContract(
        kind=kind,
        requires_plan=kind is not TaskKind.explain,
        requires_verification=requires_verification,
    )


def is_verification_evidence(tool_name: str, arguments: dict, observation: dict) -> bool:
    if "error" in observation:
        return False
    if tool_name == "wait_for_http":
        status = observation.get("status")
        return isinstance(status, int) and 200 <= status < 400
    if tool_name == "run_cli":
        command = str(arguments.get("command", "")).lower()
        markers = ("build", "curl", "lint", "test", "pytest", "dotnet run")
        return observation.get("exit_code") == 0 and any(marker in command for marker in markers)
    return False
