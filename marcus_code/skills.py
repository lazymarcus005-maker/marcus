import re
from dataclasses import dataclass
from pathlib import Path

from harness.db.enums import RiskTier
from harness.runtime.tools import Tool


@dataclass(frozen=True)
class LocalSkill:
    name: str
    description: str
    instructions: str
    path: str


def discover_local_skills(root: Path) -> list[LocalSkill]:
    skills_root = root / ".marcus" / "skills"
    if not skills_root.is_dir():
        return []
    found: list[LocalSkill] = []
    for path in sorted(skills_root.glob("*/SKILL.md")):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        name, description, body = _parse_skill_markdown(raw)
        if name and description and body:
            found.append(LocalSkill(name, description, body, str(path.relative_to(root))))
    return found


def build_skill_catalog(root: Path) -> str:
    skills = discover_local_skills(root)
    if not skills:
        return ""
    lines = ["\n\nLocal skills (use load_skill with the exact name when relevant):"]
    lines.extend(f"- {skill.name}: {skill.description}" for skill in skills[:50])
    return "\n".join(lines)


def build_load_skill_tool(root: Path) -> Tool:
    async def handler(arguments: dict) -> dict:
        requested = arguments.get("name")
        if not requested:
            raise ValueError("load_skill requires a 'name' field")
        for skill in discover_local_skills(root):
            if skill.name == requested:
                return {
                    "name": skill.name,
                    "description": skill.description,
                    "instructions": skill.instructions,
                    "path": skill.path,
                }
        raise ValueError(f"unknown local skill: {requested}")

    return Tool(
        name="load_skill",
        description="Load a local SKILL.md instruction set by exact name.",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Exact skill name."}},
            "required": ["name"],
        },
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


def _parse_skill_markdown(raw: str) -> tuple[str, str, str]:
    if not raw.startswith("---\n"):
        return "", "", ""
    end = raw.find("\n---", 4)
    if end < 0:
        return "", "", ""
    values: dict[str, str] = {}
    for line in raw[4:end].splitlines():
        match = re.match(r"^(name|description):\s*(.+?)\s*$", line)
        if match:
            values[match.group(1)] = match.group(2).strip().strip("\"'")
    return values.get("name", ""), values.get("description", ""), raw[end + 4 :].strip()
