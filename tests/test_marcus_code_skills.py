import pytest

from marcus_code.skills import build_load_skill_tool, build_skill_catalog, discover_local_skills


def _write_skill(root, name="review", description="Review code safely"):
    path = root / ".marcus" / "skills" / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f'---\nname: {name}\ndescription: "{description}"\n---\n\nInspect first, then explain findings.',
        encoding="utf-8",
    )


def test_discover_local_skills_and_catalog(tmp_path):
    _write_skill(tmp_path)
    skills = discover_local_skills(tmp_path)
    assert skills[0].name == "review"
    assert "review: Review code safely" in build_skill_catalog(tmp_path)


@pytest.mark.asyncio
async def test_load_skill_returns_instructions(tmp_path):
    _write_skill(tmp_path)
    result = await build_load_skill_tool(tmp_path).handler({"name": "review"})
    assert "Inspect first" in result["instructions"]


@pytest.mark.asyncio
async def test_load_skill_rejects_unknown_name(tmp_path):
    with pytest.raises(ValueError, match="unknown"):
        await build_load_skill_tool(tmp_path).handler({"name": "missing"})
