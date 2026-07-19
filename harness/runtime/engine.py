import asyncio
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from harness.config import get_settings
from harness.db.enums import (
    TERMINAL_RUN_STATUSES,
    MessageRole,
    RiskTier,
    RunStatus,
    StepType,
    ToolExecutionStatus,
)
from harness.db.models import AgentRun, AgentStep, ToolExecution
from harness.llm.gateway import LLMError, LLMGateway
from harness.llm.types import ToolCall
from harness.llm.usage import record_usage
from harness.mcp.registry import McpRegistry
from harness.mcp.tools import build_tool as build_mcp_tool
from harness.mcp.tools import canonical_tool_name
from harness.observability import LLM_TOKENS, RUN_DURATION, RUNS_COMPLETED, span
from harness.runtime import guardrails, native_tools
from harness.runtime.compaction import maybe_compact
from harness.runtime.context import build_llm_messages, select_skill_candidate
from harness.runtime.native_tools import (
    ASK_USER_TOOL_NAME,
    BUILTIN_DOMAIN_NAME,
    FINISH_TOOL_NAME,
    LIST_DOMAIN_TOOLS_NAME,
    LIST_TOOL_DOMAINS_NAME,
    LOAD_TOOL_NAME,
    USE_SKILL_NAME,
    build_ask_user_tool,
    build_builtin_tools,
    build_finish_tool,
)
from harness.runtime.repository import RunRepository
from harness.runtime.tool_executor import ToolExecutor
from harness.runtime.tools import Tool, ToolErrorCode, ToolRuntimeError
from harness.skills.registry import SkillRegistry
from harness.skills.usage import record_skill_usage_for_run

# Control results returned by _process_tool_calls, consumed by _execute_step.
ControlResult = tuple[str, dict[str, Any]] | None


def _build_deferred_tool(original: Tool | None, error_message: str) -> Tool:
    """Build a policy-denied placeholder tool for deferred batched calls.

    Defined outside the loop so Ruff's B023 closure check is satisfied.
    """

    async def deny_batched_call(arguments: dict[str, Any]) -> dict[str, Any]:
        raise ToolRuntimeError(
            error_message,
            code=ToolErrorCode.policy_denied,
            retryable=True,
        )

    return Tool(
        name=original.name if original else "unknown",
        description="Deferred batched tool call.",
        parameters=original.parameters if original else {"type": "object"},
        handler=deny_batched_call,
        risk_tier=original.risk_tier if original else RiskTier.read_only,
        idempotent=True,
    )


async def _unused_meta_tool_handler(arguments: dict[str, Any]) -> dict[str, Any]:
    """Placeholder for the Tool objects used only to build LLM-facing specs.

    _process_tool_calls always special-cases these tool names before any
    dispatch through tools_by_name, so this should never actually run — the
    real, DB-bound handler is built fresh per call in _process_tool_calls.
    """
    raise AssertionError("meta-tool placeholder handler invoked directly")


async def _not_loaded_handler(arguments: dict[str, Any]) -> dict[str, Any]:
    return {"error": "tool is not loaded yet; call load_tool with its name first"}


class RunEngine:
    """The ReAct loop: load state -> LLM -> validate -> execute tools -> persist -> loop.

    One run_until_blocked() call advances a run step by step until it hits a
    terminal state (completed/failed/cancelled/timed_out), a waiting state
    (waiting_user_input/waiting_approval), or a guardrail stops it. It never
    raises for run-level failures — those become a checkpointed status with
    run.error set; only truly unexpected errors (e.g. the run not existing)
    raise.
    """

    def __init__(
        self,
        session: AsyncSession,
        llm: LLMGateway,
        tools: list[Tool] | None = None,
        mcp_registry: McpRegistry | None = None,
    ) -> None:
        self.session = session
        self.settings = get_settings()
        self.repo = RunRepository(session)
        self.llm = llm
        self.tool_executor = ToolExecutor(session)
        self.tools_by_name = {t.name: t for t in (tools or [])}
        # Built-in capability tools (fetch_url/read_file/write_file/run_cli)
        # are always available, exposed as the synthetic "builtin" domain via
        # the same progressive-disclosure flow as MCP servers (see
        # _build_list_domains_tool/_build_load_tool_tool below). setdefault
        # so tests passing an explicit `tools=` with the same name still win.
        for builtin_tool in build_builtin_tools(self.settings):
            self.tools_by_name.setdefault(builtin_tool.name, builtin_tool)
        self._finish_tool = build_finish_tool()
        self._ask_user_tool = build_ask_user_tool()
        self._list_domains_tool = native_tools.build_list_tool_domains_tool(
            _unused_meta_tool_handler
        )
        self._list_domain_tools_tool = native_tools.build_list_domain_tools_tool(
            _unused_meta_tool_handler
        )
        self._load_tool_tool = native_tools.build_load_tool_tool(_unused_meta_tool_handler)
        self._use_skill_tool = native_tools.build_use_skill_tool(_unused_meta_tool_handler)
        # MCP tools (#14) are discovered from the DB, not passed in statically
        # like `tools` — loaded lazily on first LLM call and cached for the
        # life of this engine instance (one instance per run, see workers/main.py).
        self.mcp_registry = mcp_registry or McpRegistry(session)
        self.skill_registry = SkillRegistry(session)
        self._mcp_tools_loaded = False

    async def run_until_blocked(self, run_id: uuid.UUID) -> AgentRun:
        with span("agent.run", run_id=str(run_id)):
            run = await self.repo.get_with_history(run_id)
            if run is None:
                raise ValueError(f"run {run_id} not found")

            if run.status == RunStatus.pending:
                run = await self.repo.checkpoint(run, status=RunStatus.running)
                await self.session.commit()

            while run.status == RunStatus.running:
                run = await self._execute_step(run)

            if run.status in TERMINAL_RUN_STATUSES:
                status = RunStatus(run.status)
                RUNS_COMPLETED.labels(status=status.value).inc()
                RUN_DURATION.labels(status=status.value).observe(
                    max(0.0, (run.updated_at - run.created_at).total_seconds())
                )
            return run

    async def _execute_step(self, run: AgentRun) -> AgentRun:
        with span(
            "agent.step", run_id=str(run.id), tenant_id=str(run.tenant_id), step_no=run.current_step
        ):
            return await self._execute_step_inner(run)

    async def _execute_step_inner(self, run: AgentRun) -> AgentRun:
        try:
            guardrails.check_before_step(run)
        except guardrails.GuardrailViolation as violation:
            run = await self.repo.checkpoint(run, status=violation.status, error=violation.reason)
            await record_skill_usage_for_run(self.session, run)
            await self.session.commit()
            return run

        # Must happen unconditionally, not just inside _call_llm: on a
        # crash-recovery replay (existing_step is not None below) the LLM is
        # never re-called, but _process_tool_calls still needs tools_by_name
        # populated to execute a persisted MCP tool call.
        await self._ensure_mcp_tools_loaded(run.tenant_id)

        step_no = run.current_step
        existing_step = await self._get_step(run.id, step_no)

        if existing_step is None:
            outcome = await self._call_llm(run, step_no)
            if outcome is None:
                # LLM call failed; the terminal checkpoint was already committed.
                run = await self.repo.get(run.id) or run
                return run
            tool_calls_payload, plain_content, tokens_used = outcome
        else:
            tool_calls_payload = existing_step.payload.get("tool_calls", [])
            plain_content = existing_step.payload.get("content")
            tokens_used = run.tokens_used

        if guardrails.token_budget_exceeded(run, tokens_used):
            run = await self.repo.checkpoint(
                run,
                status=RunStatus.timed_out,
                error="token budget exceeded",
                tokens_used=tokens_used,
                current_step=step_no + 1,
            )
            await record_skill_usage_for_run(self.session, run)
            await self.session.commit()
            return run

        if not tool_calls_payload:
            if plain_content:
                await self.repo.add_message(run.id, MessageRole.assistant, plain_content)
            run = await self.repo.checkpoint(
                run,
                status=RunStatus.waiting_user_input,
                current_step=step_no + 1,
                tokens_used=tokens_used,
            )
            await self.session.commit()
            return run

        try:
            guardrails.check_tool_call_budget(run, len(tool_calls_payload))
        except guardrails.GuardrailViolation as violation:
            run = await self.repo.checkpoint(
                run,
                status=violation.status,
                error=violation.reason,
                current_step=step_no + 1,
                tokens_used=tokens_used,
            )
            await record_skill_usage_for_run(self.session, run)
            await self.session.commit()
            return run

        control_result = await self._process_tool_calls(run, step_no, tool_calls_payload)

        field_updates: dict[str, Any] = {
            "current_step": step_no + 1,
            "tokens_used": tokens_used,
            "tool_calls_used": run.tool_calls_used + len(tool_calls_payload),
        }

        if control_result is not None and control_result[0] == "waiting_approval":
            # Compute how many calls actually executed vs how many were
            # deferred so the budget reflects reality after resume.
            executed_count = control_result[1].get("executed_count", 0)
            field_updates["tool_calls_used"] = run.tool_calls_used + executed_count
            field_updates["current_step"] = step_no  # do not advance
            # Unlike every other branch, current_step must NOT advance: on
            # resume the engine has to replay this exact step_no's persisted
            # tool_calls (already-executed calls short-circuit via their
            # idempotency keys, the just-decided one now resolves, and any
            # calls after it in the batch proceed normally). See issue #17.
            run = await self.repo.checkpoint(
                run, status=RunStatus.waiting_approval, **field_updates
            )
            await self.session.commit()
            return run

        if control_result is None:
            run = await self.repo.checkpoint(run, **field_updates)
            await self.session.commit()
            return run

        kind, args = control_result
        if kind == "completed":
            run = await self.repo.checkpoint(
                run, status=RunStatus.completed, final_result=args, **field_updates
            )
            await record_skill_usage_for_run(self.session, run)
        elif kind == "waiting_user_input":
            await self.repo.add_message(
                run.id, MessageRole.assistant, args.get("question", "(no question provided)")
            )
            run = await self.repo.checkpoint(
                run, status=RunStatus.waiting_user_input, **field_updates
            )
        elif kind == "failed":
            run = await self.repo.checkpoint(
                run,
                status=RunStatus.failed,
                error=args.get("error", "unknown error"),
                **field_updates,
            )
            await record_skill_usage_for_run(self.session, run)
        else:
            raise AssertionError(f"unhandled control result kind: {kind}")

        await self.session.commit()
        return run

    async def _call_llm(
        self, run: AgentRun, step_no: int
    ) -> tuple[list[dict], str | None, int] | None:
        """Call the LLM and persist the resulting step. Returns None if the call failed

        (in which case the run has already been checkpointed to Failed and committed).
        """
        compact_usage = await maybe_compact(
            self.session,
            self.llm,
            run,
            context_window_tokens=self.settings.run_context_window_tokens,
            threshold_fraction=self.settings.run_compact_threshold_percent / 100,
        )
        if run.active_skill_revision_id is None:
            candidate = await select_skill_candidate(self.session, run)
            if candidate is not None:
                _skill, revision = candidate
                available = [name for name in revision.required_tools if name in self.tools_by_name]
                if len(available) == len(revision.required_tools):
                    run = await self.repo.checkpoint(
                        run,
                        active_skill_revision_id=revision.id,
                        active_tool_names=available,
                    )
                    await self.session.commit()
        messages = await build_llm_messages(self.session, run)
        # Progressive disclosure (issue #15): an MCP tool's full schema is only
        # ever sent to the LLM after `load_tool` unlocked it for this run — see
        # run.active_tool_names. finish/ask_user/the three meta-tools are
        # always visible.
        unlocked_tools = (
            self.tools_by_name[name] for name in run.active_tool_names if name in self.tools_by_name
        )
        tool_specs = [
            t.to_spec()
            for t in (
                self._finish_tool,
                self._ask_user_tool,
                self._list_domains_tool,
                self._list_domain_tools_tool,
                self._load_tool_tool,
                self._use_skill_tool,
                *unlocked_tools,
            )
        ]

        with span(
            "agent.llm_call", run_id=str(run.id), tenant_id=str(run.tenant_id), step_no=step_no
        ):
            try:
                response = await self.llm.complete(messages, tools=tool_specs)
            except LLMError as exc:
                run = await self.repo.checkpoint(run, status=RunStatus.failed, error=str(exc))
                await record_skill_usage_for_run(self.session, run)
                await self.session.commit()
                return None
        LLM_TOKENS.labels(model=response.model, kind="prompt").inc(response.usage.prompt_tokens)
        LLM_TOKENS.labels(model=response.model, kind="completion").inc(
            response.usage.completion_tokens
        )

        tool_calls_payload = [
            {"id": c.id, "name": c.name, "arguments": c.arguments} for c in response.tool_calls
        ]
        payload = {
            "content": response.content,
            "tool_calls": tool_calls_payload,
            "finish_reason": response.finish_reason,
            "model": response.model,
        }
        if response.provider_fields:
            payload["provider_fields"] = response.provider_fields
        await self.repo.add_step(
            run.id,
            step_no,
            StepType.llm_call,
            payload,
            token_usage={
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
                "reasoning_tokens": response.usage.reasoning_tokens,
            },
        )
        await record_usage(
            self.session,
            tenant_id=run.tenant_id,
            run_id=run.id,
            model=response.model,
            usage=response.usage,
        )

        tokens_used = (
            run.tokens_used
            + (compact_usage.total_tokens if compact_usage else 0)
            + response.usage.total_tokens
        )
        return tool_calls_payload, response.content, tokens_used

    async def _ensure_mcp_tools_loaded(self, tenant_id: uuid.UUID) -> None:
        """Merge the tenant's enabled MCP tools into tools_by_name, once per

        engine instance (one instance per run — see workers/main.py). Doesn't
        overwrite a tool of the same name passed in statically at construction.
        """
        if self._mcp_tools_loaded:
            return
        pairs = await self.mcp_registry.list_tools(tenant_id)
        counts: dict[str, int] = {}
        for _server, mcp_tool in pairs:
            counts[mcp_tool.name] = counts.get(mcp_tool.name, 0) + 1
        for server, mcp_tool in pairs:
            exposed_name = (
                canonical_tool_name(server, mcp_tool.name)
                if counts[mcp_tool.name] > 1
                else mcp_tool.name
            )
            self.tools_by_name.setdefault(
                exposed_name,
                build_mcp_tool(
                    server,
                    mcp_tool,
                    self.mcp_registry.client,
                    exposed_name=exposed_name,
                ),
            )
        self._mcp_tools_loaded = True

    def _plan_tool_calls(
        self, tool_calls_payload: list[dict]
    ) -> tuple[list[tuple[int, dict]], list[tuple[int, dict]]]:
        """Select calls to execute now vs defer.

        Read-only calls may run concurrently up to ``run_max_tool_calls_per_step``
        when they appear first. Mutation calls run one at a time; any calls
        after the first mutation (or beyond the read-only concurrency limit)
        are deferred with a policy-denied observation.
        """
        if not tool_calls_payload:
            return [], []

        first_call = tool_calls_payload[0]
        first_name = first_call.get("name", "")
        first_tool = self.tools_by_name.get(first_name)
        if first_tool is None or first_tool.risk_tier != RiskTier.read_only:
            # First call is not read-only: execute only it now.
            return [(0, first_call)], [
                (idx, call) for idx, call in enumerate(tool_calls_payload) if idx != 0
            ]

        selected: list[tuple[int, dict]] = []
        deferred: list[tuple[int, dict]] = []
        for idx, raw_call in enumerate(tool_calls_payload):
            name = raw_call.get("name", "")
            tool = self.tools_by_name.get(name)
            if tool is None or tool.risk_tier != RiskTier.read_only:
                deferred.append((idx, raw_call))
                continue
            if len(selected) < self.settings.run_max_tool_calls_per_step:
                selected.append((idx, raw_call))
            else:
                deferred.append((idx, raw_call))
        return selected, deferred

    async def _process_tool_calls(
        self, run: AgentRun, step_no: int, tool_calls_payload: list[dict]
    ) -> ControlResult:
        selected, deferred = self._plan_tool_calls(tool_calls_payload)

        # Record policy-denied observations for deferred calls up front.
        for call_index, raw_call in deferred:
            call = ToolCall(
                id=raw_call["id"], name=raw_call["name"], arguments=raw_call["arguments"]
            )

            original = self.tools_by_name.get(call.name)
            is_read_only = original is not None and original.risk_tier == RiskTier.read_only
            error_message = (
                f"up to {self.settings.run_max_tool_calls_per_step} read-only tool calls "
                "are allowed per reasoning step"
                if is_read_only
                else (
                    "only one mutation tool call is allowed per reasoning step; "
                    "inspect its result before choosing the next action"
                )
            )
            denied_tool = _build_deferred_tool(original, error_message)
            await self.tool_executor.execute(run, step_no, call_index, call, tool=denied_tool)

        # Execute selected calls. Read-only calls can run concurrently; the
        # rest are sequential. Meta/control tools are always sequential.
        executed_count = 0
        read_only_calls: list[tuple[int, ToolCall]] = []
        sequential_calls: list[tuple[int, ToolCall]] = []
        for call_index, raw_call in selected:
            call = ToolCall(
                id=raw_call["id"], name=raw_call["name"], arguments=raw_call["arguments"]
            )
            tool = self.tools_by_name.get(call.name)
            if tool is not None and tool.risk_tier == RiskTier.read_only:
                read_only_calls.append((call_index, call))
            else:
                sequential_calls.append((call_index, call))

        async def _run_read_only(pair: tuple[int, ToolCall]) -> tuple[int, Any]:
            call_index, call = pair
            return call_index, await self._execute_single_tool_call(run, step_no, call_index, call)

        if read_only_calls:
            read_only_results = await asyncio.gather(
                *(_run_read_only(pair) for pair in read_only_calls), return_exceptions=True
            )
            for item in read_only_results:
                if isinstance(item, BaseException):
                    # Should not happen because _execute_single_tool_call
                    # captures its own exceptions, but guard just in case.
                    continue
                _, control = item
                if control is not None and control[0] == "waiting_approval":
                    return "waiting_approval", {"executed_count": executed_count}
                executed_count += 1

        for call_index, call in sequential_calls:
            control = await self._execute_single_tool_call(run, step_no, call_index, call)
            if control is not None:
                if control[0] == "waiting_approval":
                    return "waiting_approval", {"executed_count": executed_count}
                return control[0], {**control[1], "executed_count": executed_count}
            executed_count += 1

        return None

    async def _execute_single_tool_call(
        self, run: AgentRun, step_no: int, call_index: int, call: ToolCall
    ) -> ControlResult:
        if call.name == FINISH_TOOL_NAME:
            evidence_error = await self._finish_evidence_error(run, call.arguments)
            finish_tool = self._finish_tool
            if evidence_error:

                async def reject_finish(
                    arguments: dict[str, Any], error: str = evidence_error
                ) -> dict[str, Any]:
                    raise ValueError(error)

                finish_tool = Tool(
                    name=self._finish_tool.name,
                    description=self._finish_tool.description,
                    parameters=self._finish_tool.parameters,
                    handler=reject_finish,
                    risk_tier=self._finish_tool.risk_tier,
                    idempotent=True,
                )
            outcome = await self.tool_executor.execute(
                run, step_no, call_index, call, tool=finish_tool
            )
            if "error" not in outcome.observation:
                return "completed", outcome.observation
            return None

        if call.name == ASK_USER_TOOL_NAME:
            outcome = await self.tool_executor.execute(
                run, step_no, call_index, call, tool=self._ask_user_tool
            )
            if "error" not in outcome.observation:
                return "waiting_user_input", outcome.observation
            return None

        if call.name == LIST_TOOL_DOMAINS_NAME:
            await self.tool_executor.execute(
                run, step_no, call_index, call, tool=self._build_list_domains_tool(run)
            )
            return None

        if call.name == LIST_DOMAIN_TOOLS_NAME:
            await self.tool_executor.execute(
                run, step_no, call_index, call, tool=self._build_list_domain_tools_tool(run)
            )
            return None

        if call.name == LOAD_TOOL_NAME:
            outcome = await self.tool_executor.execute(
                run, step_no, call_index, call, tool=self._build_load_tool_tool(run)
            )
            loaded_name = outcome.observation.get("name")
            if loaded_name and loaded_name not in run.active_tool_names:
                run = await self.repo.checkpoint(
                    run, active_tool_names=[*run.active_tool_names, loaded_name]
                )
            return None

        if call.name == USE_SKILL_NAME:
            outcome = await self.tool_executor.execute(
                run, step_no, call_index, call, tool=self._build_use_skill_tool(run)
            )
            revision_id = outcome.observation.get("revision_id")
            required_tools = outcome.observation.get("required_tools", [])
            if revision_id and "error" not in outcome.observation:
                active_tool_names = list(run.active_tool_names)
                for tool_name in required_tools:
                    if tool_name not in active_tool_names:
                        active_tool_names.append(tool_name)
                run = await self.repo.checkpoint(
                    run,
                    active_skill_revision_id=uuid.UUID(revision_id),
                    active_tool_names=active_tool_names,
                )
            return None

        tool = self.tools_by_name.get(call.name)

        if (
            tool is not None
            and tool.mcp_server_id is not None
            and call.name not in run.active_tool_names
        ):
            # Hallucinated call to an MCP tool that exists but hasn't been
            # unlocked via load_tool yet (issue #15) — its full schema was
            # never sent to the LLM, so treat it as not-yet-callable
            # rather than actually invoking it.
            guard_tool = Tool(
                name=tool.name,
                description=tool.description,
                parameters=tool.parameters,
                handler=_not_loaded_handler,
            )
            await self.tool_executor.execute(run, step_no, call_index, call, tool=guard_tool)
            return None

        if tool is not None:
            try:
                await guardrails.check_repeated_calls(
                    self.session, run.id, tool.name, call.arguments
                )
            except guardrails.GuardrailViolation as violation:
                return "failed", {"error": violation.reason}

        outcome = await self.tool_executor.execute(run, step_no, call_index, call, tool=tool)
        if outcome.needs_approval:
            return "waiting_approval", {}
        if outcome.fatal:
            return "failed", {"error": outcome.fatal_reason}
        if outcome.observation.get("code") == "INVALID_ARGUMENT":
            result = await self.session.execute(
                sa.select(sa.func.count())
                .select_from(ToolExecution)
                .where(
                    ToolExecution.run_id == run.id,
                    ToolExecution.tool_name == call.name,
                    ToolExecution.error.like("INVALID_ARGUMENT%"),
                )
            )
            failures = result.scalar_one()
            if failures > self.settings.run_max_argument_repairs:
                return "failed", {
                    "error": (
                        f"argument repair budget exhausted for {call.name!r} "
                        f"({self.settings.run_max_argument_repairs})"
                    )
                }
        try:
            await guardrails.check_no_progress(self.session, run.id)
        except guardrails.GuardrailViolation as violation:
            return "failed", {"error": violation.reason}

        return None

    async def _finish_evidence_error(self, run: AgentRun, arguments: dict[str, Any]) -> str | None:
        """Prevent a success claim while the most recent real tool failure is unresolved."""
        if arguments.get("outcome", "succeeded") == "failed":
            return None

        control_tools = {
            FINISH_TOOL_NAME,
            ASK_USER_TOOL_NAME,
            LIST_TOOL_DOMAINS_NAME,
            LIST_DOMAIN_TOOLS_NAME,
            LOAD_TOOL_NAME,
            USE_SKILL_NAME,
        }
        result = await self.session.execute(
            sa.select(ToolExecution)
            .where(
                ToolExecution.run_id == run.id,
                ToolExecution.tool_name.not_in(control_tools),
            )
            .order_by(ToolExecution.step_no.desc(), ToolExecution.call_index.desc())
            .limit(1)
        )
        latest = result.scalar_one_or_none()
        if latest is not None and latest.status != ToolExecutionStatus.succeeded:
            return (
                f"finish blocked: latest tool {latest.tool_name!r} has status "
                f"{latest.status}; recover or finish with outcome='failed'"
            )
        return None

    def _builtin_tools(self) -> list[Tool]:
        return [t for t in self.tools_by_name.values() if t.mcp_server_id is None]

    def _build_list_domains_tool(self, run: AgentRun) -> Tool:
        async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
            servers = await self.mcp_registry.list_servers(run.tenant_id)
            counts: dict[str, int] = {}
            for server, _mcp_tool in await self.mcp_registry.list_tools(run.tenant_id):
                counts[server.name] = counts.get(server.name, 0) + 1
            domains = [
                {"name": s.name, "tool_count": counts.get(s.name, 0)} for s in servers if s.enabled
            ]
            builtin_tools = self._builtin_tools()
            if builtin_tools:
                domains.append({"name": BUILTIN_DOMAIN_NAME, "tool_count": len(builtin_tools)})
            return {"domains": domains}

        return native_tools.build_list_tool_domains_tool(handler)

    def _build_list_domain_tools_tool(self, run: AgentRun) -> Tool:
        async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
            domain = arguments.get("domain")
            if domain == BUILTIN_DOMAIN_NAME:
                tools = [
                    {"name": t.name, "summary": t.description[:140]} for t in self._builtin_tools()
                ]
                return {"tools": tools}
            servers = await self.mcp_registry.list_servers(run.tenant_id)
            if not any(s.name == domain and s.enabled for s in servers):
                return {"error": f"unknown domain: {domain}"}
            pairs = await self.mcp_registry.list_tools(run.tenant_id)
            name_counts: dict[str, int] = {}
            for _server, mcp_tool in pairs:
                name_counts[mcp_tool.name] = name_counts.get(mcp_tool.name, 0) + 1
            tools = [
                {
                    "name": (
                        canonical_tool_name(server, mcp_tool.name)
                        if name_counts[mcp_tool.name] > 1
                        else mcp_tool.name
                    ),
                    "summary": mcp_tool.description[:140],
                }
                for server, mcp_tool in pairs
                if server.name == domain
            ]
            return {"tools": tools}

        return native_tools.build_list_domain_tools_tool(handler)

    def _build_load_tool_tool(self, run: AgentRun) -> Tool:
        async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
            requested = arguments.get("name")
            if not requested:
                return {"error": f"unknown tool: {requested}"}
            available_tool = self.tools_by_name.get(requested)
            if available_tool is not None:
                return {"name": available_tool.name, "schema": available_tool.parameters}
            return {"error": f"unknown tool: {requested}"}

        return native_tools.build_load_tool_tool(handler)

    def _build_use_skill_tool(self, run: AgentRun) -> Tool:
        async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
            requested = arguments.get("name")
            active = (
                await self.skill_registry.get_active_revision_by_skill_name(
                    run.tenant_id, requested
                )
                if requested
                else None
            )
            if active is None:
                return {"error": f"unknown published skill: {requested}"}

            skill, revision = active
            missing_tools = [
                name for name in revision.required_tools if name not in self.tools_by_name
            ]
            if missing_tools:
                return {
                    "error": "skill required tools are unavailable",
                    "missing_tools": missing_tools,
                }

            return {
                "name": skill.name,
                "revision_id": str(revision.id),
                "version": revision.version,
                "instruction": revision.instruction,
                "required_tools": revision.required_tools,
            }

        return native_tools.build_use_skill_tool(handler)

    async def _get_step(self, run_id: uuid.UUID, step_no: int) -> AgentStep | None:
        result = await self.session.execute(
            sa.select(AgentStep).where(AgentStep.run_id == run_id, AgentStep.step_no == step_no)
        )
        return result.scalar_one_or_none()
