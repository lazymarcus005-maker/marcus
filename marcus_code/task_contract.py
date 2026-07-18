import re
from dataclasses import dataclass
from enum import StrEnum


class TaskKind(StrEnum):
    """Backward-compatible primary intent used by status/UI code."""

    explain = "explain"
    change = "change"
    operate = "operate"


class ResponseMode(StrEnum):
    direct = "direct"
    agentic = "agentic"


class Capability(StrEnum):
    workspace_read = "workspace_read"
    workspace_write = "workspace_write"
    command = "command"
    process = "process"
    web = "web"


class VerificationPolicy(StrEnum):
    none = "none"
    requested = "requested"
    after_mutation = "after_mutation"


@dataclass(frozen=True)
class TaskContract:
    kind: TaskKind
    response_mode: ResponseMode = ResponseMode.direct
    capabilities: frozenset[Capability] = frozenset()
    requires_plan: bool = False
    verification_policy: VerificationPolicy = VerificationPolicy.none

    @property
    def requires_verification(self) -> bool:
        return self.verification_policy is not VerificationPolicy.none

    @property
    def allows_mutation(self) -> bool:
        return Capability.workspace_write in self.capabilities

    def missing_evidence_hint(self, verification_succeeded: bool) -> str:
        """Return a concrete hint for the model when finalization is denied."""
        if not self.requires_verification:
            return "this task does not require verification"
        if verification_succeeded:
            return (
                "a verification tool returned success, but the final answer "
                "must explicitly summarize the verification result"
            )
        return (
            "no successful verification from the current workspace revision exists; "
            "run an available test, build, lint, HTTP, or health-check tool and report its result"
        )


_CHANGE = re.compile(
    r"(?i)\b(add|build|change|create|edit|fix|implement|install|refactor|remove|update|write)\b"
    r"|(?:เพิ่ม|แก้|สร้าง|ติดตั้ง|เขียน|ปรับปรุง|ดำเนินการ)"
)
_OPERATE = re.compile(
    r"(?i)\b(run|start|stop|deploy|serve|launch|execute)\b"
    r"|(?:รัน|เปิด|ปิด|ทดสอบระบบ|เริ่ม|หยุด)"
)
_PROCESS = re.compile(
    r"(?i)\b(service|server|serve|launch|deploy|start process|stop process)\b"
    r"|(?:เซอร์วิส|เซิร์ฟเวอร์|รันระบบ|เปิดระบบ|ปิดระบบ)"
)
_VERIFY = re.compile(
    r"(?i)\b(build|curl|lint|test|verify|verification|pytest|dotnet\s+run|npm\s+test)\b"
    r"|(?:ทดสอบ|ยืนยันผล|ตรวจสอบผล|รัน service|รันเซอร์วิส)"
)
_ANALYZE = re.compile(
    r"(?i)\b(analy[sz]e|inspect|investigate|review|audit|debug|diagnose|find|"
    r"identify|locate|list|search|read|show|plan)\b"
    r"|(?:วิเคราะห์|ตรวจโค้ด|ตรวจหา|ค้นหา|รีวิว|สำรวจ|แสดง|อ่าน|วางแผน|หาสาเหตุ)"
)
_WORKSPACE = re.compile(
    r"(?i)\b(code|codebase|git|status|repo(?:sitory)?|project|workspace|files?|"
    r"director(?:y|ies)|modules?|functions?|classes?)\b"
    r"|(?:โค้ด|โปรเจกต์|รีโป|ไฟล์|โฟลเดอร์|โมดูล|ฟังก์ชัน|คลาส|workspace)"
)
_DEICTIC_WORKSPACE = re.compile(
    r"(?i)\b(?:this|that|current|existing)\s+"
    r"(?:code|codebase|repo(?:sitory)?|project|workspace|file|module|function|class)\b"
    r"|\b(?:in|inside)\s+(?:the\s+)?(?:repo(?:sitory)?|project|workspace)\b"
    r"|(?:^|\s)(?:[\w.@-]+[\\/])+[\w.@/-]+"
    r"|\b(?:readme|pyproject|package|cargo|go)\.(?:md|toml|json|lock|mod)\b"
    r"|\b[\w.-]+\.(?:py|pyi|js|jsx|ts|tsx|cs|java|go|rs|rb|php|vue|svelte|md|toml|yaml|yml)\b"
    r"|(?:โค้ด|โปรเจกต์|รีโป|ไฟล์|โฟลเดอร์|โมดูล|ฟังก์ชัน|คลาส)(?:นี้|ปัจจุบัน)"
)
_INFORMATIONAL = re.compile(
    r"(?i)^\s*(?:please\s+)?(?:what|why|how|when|where|which|explain|describe|tell\s+me)\b"
    r"|^\s*(?:ช่วย)?(?:อธิบาย|บอก|อะไร|ทำไม|อย่างไร|คืออะไร)"
)
_WEB = re.compile(r"(?i)\b(web|website|url|http|https|online|documentation|docs)\b|(?:เว็บไซต์|เว็บ|เอกสารออนไลน์)")

# Remove explicitly negated mutation phrases before looking for affirmative
# mutation intent. This deliberately handles both English and common Thai
# phrasing used for review/plan-only requests.
_NEGATED_CHANGE = re.compile(
    r"(?i)\b(?:do\s+not|don't|dont|without|never)\s+"
    r"(?:add|build|change|create|edit|fix|implement|install|refactor|remove|update|write)\b"
    r"|(?:ยัง)?(?:ไม่ต้อง|ห้าม|อย่า)\s*(?:เพิ่ม|แก้|สร้าง|ติดตั้ง|เขียน|ปรับปรุง|ดำเนินการ)"
)
_PLAN_ONLY_CHANGE = re.compile(
    r"(?i)\b(?:plan|propose|suggest)(?:\s+(?:how\s+to|to))?\s+"
    r"(?:fix|change|improve|update|refactor)\w*\b"
    r"|(?:วางแผน|เสนอ(?:แนวทาง)?|แนะนำ(?:แนวทาง)?)\s*(?:การ)?(?:แก้|ปรับปรุง|เปลี่ยนแปลง)"
)


def _affirmative_text(user_input: str) -> str:
    without_negation = _NEGATED_CHANGE.sub(" ", user_input)
    return _PLAN_ONLY_CHANGE.sub(" ", without_negation)


def derive_task_contract(user_input: str) -> TaskContract:
    """Infer independent runtime capabilities instead of one exclusive tool bucket.

    The classifier intentionally keeps general-knowledge questions on the
    direct path. Workspace actions, commands, and repository-specific analysis
    enter the agentic path and receive only the capabilities they need.
    """

    affirmative = _affirmative_text(user_input)
    informational = bool(_INFORMATIONAL.search(user_input))
    wants_change = bool(_CHANGE.search(affirmative)) and not informational
    wants_operation = bool(_OPERATE.search(user_input)) and not informational
    wants_verification = bool(_VERIFY.search(user_input)) and not informational
    wants_analysis = bool(_ANALYZE.search(user_input))
    references_workspace = bool(_WORKSPACE.search(user_input))
    deictic_workspace = bool(_DEICTIC_WORKSPACE.search(user_input))
    wants_process = bool(_PROCESS.search(user_input))

    capabilities: set[Capability] = set()
    repository_analysis = wants_analysis and references_workspace
    if (
        repository_analysis
        or deictic_workspace
        or wants_change
        or wants_operation
        or wants_verification
    ):
        capabilities.add(Capability.workspace_read)
    if wants_change:
        capabilities.add(Capability.workspace_write)
        # Changes normally need a command/test capability for post-change verification.
        capabilities.add(Capability.command)
    if wants_operation or wants_verification:
        capabilities.add(Capability.command)
    if wants_process:
        capabilities.add(Capability.process)
    if _WEB.search(user_input):
        capabilities.add(Capability.web)

    agentic = bool(capabilities)
    response_mode = ResponseMode.agentic if agentic else ResponseMode.direct

    if wants_change:
        kind = TaskKind.change
    elif wants_operation or wants_verification:
        kind = TaskKind.operate
    else:
        kind = TaskKind.explain

    if wants_change:
        verification_policy = VerificationPolicy.after_mutation
    elif wants_verification:
        verification_policy = VerificationPolicy.requested
    else:
        verification_policy = VerificationPolicy.none

    return TaskContract(
        kind=kind,
        response_mode=response_mode,
        capabilities=frozenset(capabilities),
        requires_plan=response_mode is ResponseMode.agentic,
        verification_policy=verification_policy,
    )


_RUN_CLI_VERIFY = re.compile(
    r"(?i)(?:^|[^\w])(?:pytest|test|tests|lint|build|curl)(?:$|[^\w])"
    r"|dotnet\s+run|npm\s+test"
)


def is_verification_attempt(tool_name: str, arguments: dict) -> bool:
    if tool_name in {"run_tests", "wait_for_http", "check_url_health"}:
        return True
    if tool_name == "run_cli":
        return bool(_RUN_CLI_VERIFY.search(str(arguments.get("command", ""))))
    return False


def is_verification_evidence(tool_name: str, arguments: dict, observation: dict) -> bool:
    if "error" in observation:
        return False
    if tool_name == "run_tests":
        return observation.get("exit_code") == 0
    if tool_name == "wait_for_http":
        status = observation.get("status")
        return bool(observation.get("ready")) or (
            isinstance(status, int) and 200 <= status < 400
        )
    if tool_name == "check_url_health":
        status = observation.get("status")
        return bool(observation.get("healthy")) and (
            status is None or isinstance(status, int) and 200 <= status < 400
        )
    if tool_name == "run_cli":
        return observation.get("exit_code") == 0 and is_verification_attempt(
            tool_name, arguments
        )
    return False
