import uuid

import pytest
from mcp.types import Tool as McpToolSpec

from harness.db.enums import MessageRole, RunStatus
from harness.db.models import Tenant
from harness.mcp.registry import McpRegistry
from harness.runtime.engine import RunEngine
from harness.runtime.repository import RunRepository
from tests.fakes import FakeMcpClient, ScriptedLLMGateway, tool_call_response


async def _make_run(session, *, goal="investigate the outage"):
    tenant = Tenant(name=f"t-{uuid.uuid4()}")
    session.add(tenant)
    await session.flush()
    repo = RunRepository(session)
    run = await repo.create_run(tenant_id=tenant.id, goal=goal)
    await repo.add_message(run.id, MessageRole.user, goal)
    await session.commit()
    return run, tenant


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
async def test_full_disclosure_flow_unlocks_schema_before_call(db_session):
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
        call_results={"search_logs": {"content": "found 3 hits", "is_error": False}},
    )
    run, tenant = await _make_run(db_session)
    registry = await _register_search_domain(db_session, tenant, client)

    llm = ScriptedLLMGateway(
        [
            tool_call_response("list_tool_domains", {}),
            tool_call_response("list_domain_tools", {"domain": "search_domain"}),
            tool_call_response("load_tool", {"name": "search_logs"}),
            tool_call_response("search_logs", {"query": "500 error"}),
            tool_call_response("finish", {"result": "done"}),
        ]
    )
    engine = RunEngine(db_session, llm, mcp_registry=registry)

    final = await engine.run_until_blocked(run.id)

    assert final.status == RunStatus.completed
    assert final.active_tool_names == ["search_logs"]
    assert client.calls == [("search_domain", "search_logs", {"query": "500 error"})]

    tools_before_load = {t.name for t in llm.calls[2]["tools"]}
    tools_after_load = {t.name for t in llm.calls[3]["tools"]}
    assert "search_logs" not in tools_before_load
    assert "search_logs" in tools_after_load


@pytest.mark.asyncio
async def test_calling_unloaded_mcp_tool_is_rejected_without_invoking_handler(db_session):
    client = FakeMcpClient(
        tools_by_server={
            "search_domain": [
                McpToolSpec(name="search_logs", description="Search logs.", inputSchema={})
            ]
        }
    )
    run, tenant = await _make_run(db_session)
    registry = await _register_search_domain(db_session, tenant, client)

    llm = ScriptedLLMGateway(
        [
            tool_call_response("search_logs", {"query": "500 error"}),
            tool_call_response("finish", {"result": "handled it"}),
        ]
    )
    engine = RunEngine(db_session, llm, mcp_registry=registry)

    final = await engine.run_until_blocked(run.id)

    assert final.status == RunStatus.completed
    assert client.calls == []  # the real MCP server was never actually called

    second_call_messages = llm.calls[1]["messages"]
    tool_messages = [m for m in second_call_messages if m.role == "tool"]
    assert any("not loaded" in (m.content or "") for m in tool_messages)


@pytest.mark.asyncio
async def test_list_domain_tools_rejects_unknown_domain(db_session):
    client = FakeMcpClient(tools_by_server={})
    run, tenant = await _make_run(db_session)
    registry = await _register_search_domain(db_session, tenant, client)

    llm = ScriptedLLMGateway(
        [
            tool_call_response("list_domain_tools", {"domain": "does_not_exist"}),
            tool_call_response("finish", {"result": "done"}),
        ]
    )
    engine = RunEngine(db_session, llm, mcp_registry=registry)

    final = await engine.run_until_blocked(run.id)

    assert final.status == RunStatus.completed
    second_call_messages = llm.calls[1]["messages"]
    tool_messages = [m for m in second_call_messages if m.role == "tool"]
    assert any("unknown domain" in (m.content or "") for m in tool_messages)
