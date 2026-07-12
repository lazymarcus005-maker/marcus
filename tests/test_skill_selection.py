import uuid

import pytest
from mcp.types import Tool as McpToolSpec

from harness.db.enums import MessageRole, RunStatus
from harness.db.models import Tenant
from harness.mcp.registry import McpRegistry
from harness.runtime.engine import RunEngine
from harness.runtime.repository import RunRepository
from harness.skills.registry import SkillRegistry
from tests.fakes import FakeMcpClient, ScriptedLLMGateway, tool_call_response


async def _make_run(session, *, goal="triage the customer incident"):
    tenant = Tenant(name=f"t-{uuid.uuid4()}")
    session.add(tenant)
    await session.flush()
    repo = RunRepository(session)
    run = await repo.create_run(tenant_id=tenant.id, goal=goal)
    await repo.add_message(run.id, MessageRole.user, goal)
    await session.commit()
    return run, tenant


async def _publish_skill(
    session,
    tenant,
    *,
    name="incident_triage",
    description="Use this for production incident triage.",
    instruction="Always inspect logs before concluding.",
    required_tools=None,
):
    registry = SkillRegistry(session)
    skill = await registry.create_skill(
        tenant_id=tenant.id,
        name=name,
        description=description,
    )
    revision = await registry.create_revision(
        tenant_id=tenant.id,
        skill_id=skill.id,
        instruction=instruction,
        change_reason="initial",
        required_tools=required_tools or [],
    )
    assert revision is not None
    await registry.publish_revision(tenant.id, skill.id, revision.id)
    await session.commit()
    return skill, revision


async def _register_search_domain(session, tenant, client: FakeMcpClient):
    registry = McpRegistry(session, client=client)
    server = await registry.register(
        tenant_id=tenant.id, name="search_domain", base_url="https://mcp.internal/search"
    )
    await session.commit()
    await registry.refresh_tools(server)
    await session.commit()
    return registry


@pytest.mark.asyncio
async def test_use_skill_persists_revision_and_injects_instruction(db_session):
    run, tenant = await _make_run(db_session)
    _skill, revision = await _publish_skill(db_session, tenant)

    llm = ScriptedLLMGateway(
        [
            tool_call_response("use_skill", {"name": "incident_triage"}),
            tool_call_response("finish", {"result": "triaged with the skill"}),
        ]
    )
    engine = RunEngine(db_session, llm)

    final = await engine.run_until_blocked(run.id)

    assert final.status == RunStatus.completed
    assert final.active_skill_revision_id == revision.id

    first_system = llm.calls[0]["messages"][0].content
    assert "Published skill catalog:" in first_system
    assert "incident_triage: Use this for production incident triage." in first_system
    assert "use_skill" in {tool.name for tool in llm.calls[0]["tools"]}

    second_system = llm.calls[1]["messages"][0].content
    assert "Active skill: incident_triage v1" in second_system
    assert "Always inspect logs before concluding." in second_system


@pytest.mark.asyncio
async def test_use_skill_auto_loads_required_tools(db_session):
    client = FakeMcpClient(
        tools_by_server={
            "search_domain": [
                McpToolSpec(
                    name="search_logs",
                    description="Search logs for a query string.",
                    inputSchema={
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                )
            ]
        },
        call_results={"search_logs": {"content": "found the error", "is_error": False}},
    )
    run, tenant = await _make_run(db_session)
    await _publish_skill(db_session, tenant, required_tools=["search_logs"])
    registry = await _register_search_domain(db_session, tenant, client)

    llm = ScriptedLLMGateway(
        [
            tool_call_response("use_skill", {"name": "incident_triage"}),
            tool_call_response("search_logs", {"query": "customer incident"}),
            tool_call_response("finish", {"result": "root cause found"}),
        ]
    )
    engine = RunEngine(db_session, llm, mcp_registry=registry)

    final = await engine.run_until_blocked(run.id)

    assert final.status == RunStatus.completed
    assert final.active_tool_names == ["search_logs"]
    assert client.calls == [("search_domain", "search_logs", {"query": "customer incident"})]
    assert "search_logs" in {tool.name for tool in llm.calls[1]["tools"]}


@pytest.mark.asyncio
async def test_use_skill_with_missing_required_tool_reports_error(db_session):
    run, tenant = await _make_run(db_session)
    await _publish_skill(db_session, tenant, required_tools=["missing_tool"])

    llm = ScriptedLLMGateway(
        [
            tool_call_response("use_skill", {"name": "incident_triage"}),
            tool_call_response("finish", {"result": "handled the missing tool"}),
        ]
    )
    engine = RunEngine(db_session, llm)

    final = await engine.run_until_blocked(run.id)

    assert final.status == RunStatus.completed
    assert final.active_skill_revision_id is None

    second_call_messages = llm.calls[1]["messages"]
    tool_messages = [m for m in second_call_messages if m.role == "tool"]
    assert any("skill required tools are unavailable" in (m.content or "") for m in tool_messages)
    assert any("missing_tool" in (m.content or "") for m in tool_messages)
